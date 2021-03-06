import numpy
import torch
import common.torch
import common.summary
import common.numpy
import attacks
from normal_training import *


class AdversarialTraining(NormalTraining):
    """
    Adversarial training.
    """

    def __init__(self, model, trainset, testset, optimizer, scheduler, attack, objective, fraction=0.5, writer=common.summary.SummaryWriter(), cuda=False):
        """
        Constructor.

        :param model: model
        :type model: torch.nn.Module
        :param trainset: training set
        :type trainset: torch.utils.data.DataLoader
        :param testset: test set
        :type testset: torch.utils.data.DataLoader
        :param optimizer: optimizer
        :type optimizer: torch.optim.Optimizer
        :param scheduler: scheduler
        :type scheduler: torch.optim.LRScheduler
        :param attack: attack
        :type attack: attacks.Attack
        :param objective: objective
        :type objective: attacks.Objective
        :param fraction: fraction of adversarial examples per batch
        :type fraction: float
        :param augmentation: augmentation
        :type augmentation: imgaug.augmenters.Sequential
        :param writer: summary writer
        :type writer: torch.utils.tensorboard.SummaryWriter or TensorboardX equivalent
        :param cuda: run on CUDA device
        :type cuda: bool
        """

        assert fraction > 0
        assert fraction <= 1
        # assert isinstance(attack, attacks.Attack)
        assert isinstance(objective, attacks.objectives.Objective)
        assert getattr(attack, 'norm', None) is not None

        super(AdversarialTraining, self).__init__(model, trainset, testset, optimizer, scheduler, writer, cuda)

        self.attack = attack
        """ (attacks.Attack) Attack. """

        self.objective = objective
        """ (attacks.Objective) Objective. """

        # in code, we want fraction to be the fraction of clean samples for simplicity
        self.fraction = 1 - fraction
        """ (float) Fraction of adversarial examples ."""

        self.max_batches = 10
        """ (int) Number of batches to test adversarially on. """

        self.writer.add_text('config/attack', self.attack.__class__.__name__)
        self.writer.add_text('config/objective', self.objective.__class__.__name__)
        self.writer.add_text('config/fraction', str(fraction))

    def train(self, epoch):
        """
        Training step.

        :param epoch: epoch
        :type epoch: int
        """

        batches = len(self.trainset)

        cl_losses = None
        cl_errors = None
        cl_confidences = None

        successes = None
        losses = None
        errors = None
        confidences = None

        b = 0
        for inputs, targets in tqdm(self.trainset):

            inputs = common.torch.as_variable(inputs, self.cuda)
            # inputs = inputs.permute(0, 3, 1, 2)
            targets = common.torch.as_variable(targets, self.cuda)

            fraction = self.fraction
            split = int(fraction*inputs.size(0))
            # update fraction for correct loss computation
            fraction = split / float(inputs.size(0))

            clean_inputs = inputs[:split]
            adversarial_inputs = inputs[split:]
            clean_targets = targets[:split]
            adversarial_targets = targets[split:]

            self.model.eval()
            self.objective.set(adversarial_targets)
            adversarial_perturbations, adversarial_objectives = self.attack.run(self.model, adversarial_inputs, self.objective)
            adversarial_perturbations = common.torch.as_variable(adversarial_perturbations, self.cuda)
            adversarial_inputs = adversarial_inputs + adversarial_perturbations

            if adversarial_inputs.shape[0] < inputs.shape[0]: # fraction is not 1
                inputs = torch.cat((clean_inputs, adversarial_inputs), dim=0)
            else:
                inputs = adversarial_inputs
                # targets remain unchanged

            self.model.train()
            self.optimizer.zero_grad()
            logits = self.model(inputs)
            clean_logits = logits[:split]
            adversarial_logits = logits[split:]

            adversarial_loss = common.torch.classification_loss(adversarial_logits, adversarial_targets)
            adversarial_error = common.torch.classification_error(adversarial_logits, adversarial_targets)

            if adversarial_inputs.shape[0] < inputs.shape[0]:
                clean_loss = common.torch.classification_loss(clean_logits, clean_targets)
                clean_error = common.torch.classification_error(clean_logits, clean_targets)
                loss = (1 - fraction) * clean_loss + fraction * adversarial_loss
            else:
                clean_loss = torch.zeros(1)
                clean_error = torch.zeros(1)
                loss = adversarial_loss

            cl_losses = common.numpy.concatenate(cl_losses, common.torch.classification_loss(clean_logits, clean_targets, reduction='none').detach().cpu().numpy())
            cl_errors = common.numpy.concatenate(cl_errors, common.torch.classification_error(clean_logits, clean_targets, reduction='none').detach().cpu().numpy())
            cl_confidences = common.numpy.concatenate(cl_confidences, torch.max(torch.nn.functional.softmax(clean_logits, dim=1), dim=1)[0].detach().cpu().numpy())

            successes = common.numpy.concatenate(successes,
                                                torch.clamp(torch.abs(adversarial_targets - torch.max(
                                                torch.nn.functional.softmax(adversarial_logits, dim=1), dim=1)[1]),
                                                max=1).detach().cpu().numpy())
            losses = common.numpy.concatenate(losses, common.torch.classification_loss(adversarial_logits, adversarial_targets, reduction='none').detach().cpu().numpy())
            errors = common.numpy.concatenate(errors, common.torch.classification_error(adversarial_logits, adversarial_targets, reduction='none').detach().cpu().numpy())
            confidences = common.numpy.concatenate(confidences, torch.max(torch.nn.functional.softmax(adversarial_logits, dim=1), dim=1)[0].detach().cpu().numpy())

            loss.backward()
            self.optimizer.step()
            # self.scheduler.step()

            if b == (batches-1):
                # global_step = epoch * len(self.trainset) + b
                global_step = epoch
                # self.writer.add_scalar('train/lr', self.scheduler.get_last_lr()[0], global_step=global_step)

                # if adversarial_inputs.shape[0] < inputs.shape[0]: # fraction is not 1
                #     self.writer.add_scalar('train/loss', clean_loss.item(), global_step=global_step)
                #     self.writer.add_scalar('train/error', clean_error.item(), global_step=global_step)
                #     self.writer.add_scalar('train/confidence', torch.mean(torch.max(torch.nn.functional.softmax(clean_logits, dim=1), dim=1)[0]).item(), global_step=global_step)

                #     self.writer.add_histogram('train/logits', torch.max(clean_logits, dim=1)[0], global_step=global_step)
                #     self.writer.add_histogram('train/confidences', torch.max(torch.nn.functional.softmax(clean_logits, dim=1), dim=1)[0], global_step=global_step)
                if adversarial_inputs.shape[0] < inputs.shape[0]: # fraction is not 1
                    self.writer.add_scalar('train/loss', numpy.mean(cl_losses), global_step=global_step)
                    self.writer.add_scalar('train/error', numpy.mean(cl_errors), global_step=global_step)
                    self.writer.add_scalar('train/confidence', numpy.mean(cl_confidences), global_step=global_step)


                # success = torch.clamp(torch.abs(adversarial_targets - torch.max(torch.nn.functional.softmax(adversarial_logits, dim=1), dim=1)[1]), max=1)
                # self.writer.add_scalar('train/adversarial_loss', adversarial_loss.item(), global_step=global_step)
                # self.writer.add_scalar('train/adversarial_error', adversarial_error.item(), global_step=global_step)
                # self.writer.add_scalar('train/adversarial_confidence', torch.mean(torch.max(torch.nn.functional.softmax(adversarial_logits, dim=1), dim=1)[0]).item(), global_step=global_step)
                # self.writer.add_scalar('train/adversarial_success', torch.mean(success.float()).item(), global_step=global_step)
                self.writer.add_scalar('train/adversarial_success', numpy.mean(successes), global_step=global_step)
                self.writer.add_scalar('train/adversarial_loss', numpy.mean(losses), global_step=global_step)
                self.writer.add_scalar('train/adversarial_error', numpy.mean(errors), global_step=global_step)
                self.writer.add_scalar('train/adversarial_confidence', numpy.mean(confidences), global_step=global_step)

                # self.writer.add_histogram('train/adversarial_logits', torch.max(adversarial_logits, dim=1)[0], global_step=global_step)
                # self.writer.add_histogram('train/adversarial_confidences', torch.max(torch.nn.functional.softmax(adversarial_logits, dim=1), dim=1)[0], global_step=global_step)


                # if self.summary_gradients:
                #     for name, parameter in self.model.named_parameters():
                #         self.writer.add_histogram('train_weights/%s' % name, parameter.view(-1), global_step=global_step)
                #         self.writer.add_histogram('train_gradients/%s' % name, parameter.grad.view(-1), global_step=global_step)

                if adversarial_inputs.shape[0] < inputs.shape[0]: # fraction is not 1
                    self.writer.add_images('train/images', inputs[:min(8, split)], global_step=global_step)
                self.writer.add_images('train/adversarial_images', inputs[split:split + 8], global_step=global_step)
            # self.progress(epoch, b, len(self.trainset))
            b+=1


    def test(self, epoch):
        """
        Test on adversarial examples.

        :param epoch: epoch
        :type epoch: int
        """

        super(AdversarialTraining, self).test(epoch)

        self.model.eval()

        losses = None
        errors = None
        confidences = None
        successes = None
        norms = None
        objectives = None

        b = 0
        for (inputs, targets) in tqdm(self.testset):
            if b >= self.max_batches:
                break

            inputs = common.torch.as_variable(inputs, self.cuda)
            # inputs = inputs.permute(0, 3, 1, 2)
            targets = common.torch.as_variable(targets, self.cuda)

            self.objective.set(targets)
            adversarial_perturbations, adversarial_objectives = self.attack.run(self.model, inputs, self.objective)
            objectives = common.numpy.concatenate(objectives, adversarial_objectives)

            adversarial_perturbations = common.torch.as_variable(adversarial_perturbations, self.cuda)
            inputs = inputs + adversarial_perturbations

            logits = self.model(inputs)
            losses = common.numpy.concatenate(losses, common.torch.classification_loss(logits, targets, reduction='none').detach().cpu().numpy())
            errors = common.numpy.concatenate(errors, common.torch.classification_error(logits, targets, reduction='none').detach().cpu().numpy())
            confidences = common.numpy.concatenate(confidences, torch.max(torch.nn.functional.softmax(logits, dim=1), dim=1)[0].detach().cpu().numpy())
            successes = common.numpy.concatenate(successes, torch.clamp(torch.abs(targets - torch.max(torch.nn.functional.softmax(logits, dim=1), dim=1)[1]), max=1).detach().cpu().numpy())
            norms = common.numpy.concatenate(norms, self.attack.norm(adversarial_perturbations).detach().cpu().numpy())
            # self.progress(epoch, b, self.max_batches)
            b += 1
            del inputs, targets, logits

        global_step = epoch + 1# * len(self.trainset) + len(self.trainset) - 1
        self.writer.add_scalar('test/adversarial_loss', numpy.mean(losses), global_step=global_step)
        self.writer.add_scalar('test/adversarial_error', numpy.mean(errors), global_step=global_step)
        self.writer.add_scalar('test/adversarial_confidence', numpy.mean(confidences), global_step=global_step)
        self.writer.add_scalar('test/adversarial_success', numpy.mean(successes), global_step=global_step)
        self.writer.add_scalar('test/adversarial_norm', numpy.mean(norms), global_step=global_step)
        self.writer.add_scalar('test/adversarial_objective', numpy.mean(objectives), global_step=global_step)

        # self.writer.add_histogram('test/adversarial_losses', losses, global_step=global_step)
        # self.writer.add_histogram('test/adversarial_errors', errors, global_step=global_step)
        # self.writer.add_histogram('test/adversarial_confidences', confidences, global_step=global_step)
        # self.writer.add_histogram('test/adversarial_norms', norms, global_step=global_step)
        # self.writer.add_histogram('test/adversarial_objectives', objectives, global_step=global_step)



    def step(self, epoch):
        """
        Training + test step.

        :param epoch: epoch
        :type epoch: int
        """

        self.train(epoch)
        self.test(epoch)
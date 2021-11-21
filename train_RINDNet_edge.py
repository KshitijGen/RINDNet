import os
from tqdm import tqdm
from utils.lr_scheduler import LR_Scheduler
from dataloaders.datasets.bsds_hd5_dim1 import Mydataset
from torch.utils.data import DataLoader
from my_options.RINDNet_options import myNet_Options
from modeling.rindnet_edge import MyNet
from modeling.sync_batchnorm.replicate import patch_replication_callback
from utils.edge_loss2 import *
from utils.saver import Saver
from utils.summaries import TensorboardSummary
import scipy.io as sio

class Trainer(object):
    def __init__(self, args):
        self.args = args

        # Define Saver
        self.saver = Saver(args)
        self.saver.save_experiment_config()

        # Define Tensorboard Summary
        self.summary = TensorboardSummary(self.saver.experiment_dir)
        self.writer = self.summary.create_summary()

        print(self.saver.experiment_dir)
        self.output_dir = os.path.join(self.saver.experiment_dir)
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        # Define Dataloader
        self.train_dataset = Mydataset(root_path=self.args.data_path, split='trainval', crop_size=self.args.crop_size)
        self.test_dataset = Mydataset(root_path=self.args.data_path, split='test', crop_size=self.args.crop_size)
        self.train_loader = DataLoader(self.train_dataset, batch_size=self.args.batch_size, shuffle=True,
                                       num_workers=args.workers, pin_memory=True, drop_last=True)
        self.test_loader = DataLoader(self.test_dataset, batch_size=1, shuffle=False,
                                      num_workers=args.workers)

        # Define network
        self.model = MyNet()
        if self.args.resnet:
            self.model.load_resnet(args.resnet)

        # Using cuda
        if self.args.cuda:
            self.model = torch.nn.DataParallel(self.model, device_ids=self.args.gpu_ids)
            patch_replication_callback(self.model)
            self.model = self.model.cuda()

        # Define Criterion
        self.criterion3 = AttentionLossSingleMap()
        # Define Optimizer
        self.optimizer = torch.optim.SGD(self.model.parameters(), lr=self.args.lr, momentum=self.args.momentum,
                                         weight_decay=self.args.weight_decay)
        # Define lr scheduler
        self.scheduler = LR_Scheduler(self.args.lr_scheduler, self.args.lr,
                                      args.epochs, len(self.train_loader))

        # Resuming checkpoint
        self.best_pred = 0.0
        if args.resume is not None:
            if not os.path.isfile(args.resume):
                raise RuntimeError("=> no checkpoint found at '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            if args.cuda:
                self.model.module.load_state_dict(checkpoint['state_dict'])
            else:
                self.model.load_state_dict(checkpoint['state_dict'])
            if not args.ft:
                self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.best_pred = checkpoint['best_pred']
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))

    def training(self, epoch):
        train_loss = 0.0
        self.model.train()
        tbar = tqdm(self.train_loader)
        num_img_tr = len(self.train_loader)
        for i, (image, target) in enumerate(tbar):
            if self.args.cuda:
                image, target = image.cuda(), target.cuda()  # (b,3,w,h) (b,5,w,h)
                target = target.unsqueeze(1)
            all_edge = self.model(image)
            total_loss = self.criterion3(all_edge, target)

            self.scheduler(self.optimizer, i, epoch, self.best_pred)
            self.optimizer.zero_grad()
            total_loss.backward()
            self.optimizer.step()

            train_loss += total_loss.item()
            tbar.set_description('Train loss: %.3f' % (train_loss / (i + 1)))
            self.writer.add_scalar('train/total_loss_iter', total_loss.item(), i + num_img_tr * epoch)

        self.writer.add_scalar('train/total_loss_epoch', train_loss, epoch)
        print('[Epoch: %d, numImages: %5d]' % (epoch, i * self.args.batch_size + image.data.shape[0]))
        print('Loss: %.3f' % train_loss)

        if self.args.no_val:
            # save checkpoint every epoch
            if (epoch + 1) % 10 == 0:
                is_best = False
                self.saver.save_checkpoint({
                    'epoch': epoch + 1,
                    'state_dict': self.model.module.state_dict(),
                    'optimizer': self.optimizer.state_dict(),
                    'best_pred': self.best_pred,
                }, is_best)

    def test(self, epoch):
        print('Test epoch: %d' % epoch)
        self.output_dir = os.path.join(self.saver.experiment_dir, str(epoch+1))
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        self.alledge_output_dir = os.path.join(self.saver.experiment_dir, str(epoch + 1), 'mat')
        if not os.path.exists(self.alledge_output_dir):
            os.makedirs(self.alledge_output_dir)
        self.model.eval()
        tbar = tqdm(self.test_loader, desc='\r')
        for i, image in enumerate(tbar):
            name = self.test_loader.dataset.images_name[i]
            if self.args.cuda:
                image = image.cuda()
            with torch.no_grad():
                all_edge = self.model(image)

            all_edge_pred = all_edge.data.cpu().numpy()
            all_edge_pred = all_edge_pred.squeeze()
            sio.savemat(os.path.join(self.alledge_output_dir, '{}.mat'.format(name)),
                        {'result': all_edge_pred})


def main():
    options = myNet_Options()
    args = options.parse()
    #args.cuda = True
    args.cuda = not args.no_cuda and torch.cuda.is_available()
    if args.cuda:
        try:
            args.gpu_ids = [int(s) for s in args.gpu_ids.split(',')]
        except ValueError:
            raise ValueError('Argument --gpu_ids must be a comma-separated list of integers only')

    if args.sync_bn is None:
        if args.cuda and len(args.gpu_ids) > 1:
            args.sync_bn = True
        else:
            args.sync_bn = False

    args.data_path = 'data/BSDS-RIND/BSDS-RIND-Edge/Augmentation/'
    print(args)

    torch.manual_seed(args.seed)
    trainer = Trainer(args)
    print('Starting Epoch:', trainer.args.start_epoch)
    print('Total Epoches:', trainer.args.epochs)
    for epoch in range(trainer.args.start_epoch, trainer.args.epochs):
        #trainer.test(epoch)
        trainer.training(epoch)
        if (epoch+1)%10==0:
            trainer.test(epoch)


if __name__ == "__main__":
    main()

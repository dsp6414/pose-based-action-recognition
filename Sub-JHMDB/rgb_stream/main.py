import numpy as np
import pickle
from PIL import Image
import time
from tqdm import tqdm
import shutil
from random import randint
import argparse

from torch.utils.data import Dataset, DataLoader
import torchvision.transforms as transforms
import torchvision.models as models
import torch.nn as nn
import torch
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
from torch.optim.lr_scheduler import ReduceLROnPlateau

from util import *
from network import *
from dataloader import *


os.environ["CUDA_VISIBLE_DEVICES"] = "3"

parser = argparse.ArgumentParser(description='PyTorch ResNet3D on Sub-JHMDB')
parser.add_argument('--epochs', default=500, type=int, metavar='N', help='number of total epochs')
parser.add_argument('--batch-size', default=32, type=int, metavar='N', help='mini-batch size (default: 64)')
parser.add_argument('--lr', default=1e-6, type=float, metavar='LR', help='initial learning rate')
parser.add_argument('--evaluate', dest='evaluate', action='store_true', help='evaluate model on validation set')
parser.add_argument('--resume', default='', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N', help='manual epoch number (useful on restarts)')

def main():
    global arg
    arg = parser.parse_args()
    print arg

    #Prepare DataLoader
    data_loader =RGB_DataLoader(
                        BATCH_SIZE=arg.batch_size,
                        num_workers=8,
                        dic_path='/home/ubuntu/cvlab/pytorch/Sub-JHMDB_pose_stream/get_train_test_split/dict/',
                        data_path='/home/ubuntu/data/JHMDB/Rename_Images/',
                        )
    
    train_loader, val_loader = data_loader.run()
    #Model 
    model = RGB_ResNet(
                        nb_epochs=arg.epochs,
                        lr=arg.lr,
                        batch_size=arg.batch_size,
                        resume=arg.resume,
                        start_epoch=arg.start_epoch,
                        evaluate=arg.evaluate,
                        train_loader=train_loader,
                        val_loader=val_loader)
    #Training
    model.run()

class RGB_ResNet():

    def __init__(self, nb_epochs, lr, batch_size, resume, start_epoch, evaluate, train_loader, val_loader):
        self.nb_epochs=nb_epochs
        self.lr=lr
        self.batch_size=batch_size
        self.resume=resume
        self.start_epoch=start_epoch
        self.evaluate=evaluate
        self.train_loader=train_loader
        self.val_loader=val_loader
        self.best_prec1=0

    def build_model(self):
        print ('==> Build model and setup loss and optimizer')
        #build model
        self.model = resnet50(pretrained=True).cuda()
        #print self.model
        #Loss function and optimizer
        self.criterion = nn.CrossEntropyLoss().cuda()
        #self.optimizer = torch.optim.SGD(self.model.parameters(), self.lr, momentum=0.9)
        self.optimizer = torch.optim.Adam(self.model.parameters(), self.lr)
        self.scheduler = ReduceLROnPlateau(self.optimizer, 'min', patience=2,verbose=True)

    def resume_and_evaluate(self):
        if self.resume:
            if os.path.isfile(self.resume):
                print("==> loading checkpoint '{}'".format(self.resume))
                checkpoint = torch.load(self.resume)
                self.start_epoch = checkpoint['epoch']
                self.best_prec1 = checkpoint['best_prec1']
                self.model.load_state_dict(checkpoint['state_dict'])
                self.optimizer.load_state_dict(checkpoint['optimizer'])
                print("==> loaded checkpoint '{}' (epoch {}) (best_prec1 {})"
                  .format(self.resume, checkpoint['epoch'], self.best_prec1))
            else:
                print("==> no checkpoint found at '{}'".format(self.resume))
        if self.evaluate:
            prec1, val_loss = self.validate_1epoch()
    
    def run(self):
        self.build_model()
        self.resume_and_evaluate()


        cudnn.benchmark = True
        for self.epoch in range(self.start_epoch, self.nb_epochs):
            print('==> Epoch:[{0}/{1}][training stage]'.format(self.epoch, self.nb_epochs))
            train_info=self.train_1epoch()
            #record_logger(self.train_logger,train_info,self.epoch)
            print('==> Epoch:[{0}/{1}][validation stage]'.format(self.epoch, self.nb_epochs))
            prec1, val_loss, val_info = self.validate_1epoch()
            #record_logger(self.val_logger,val_info,self.epoch)

            self.scheduler.step(val_loss)
            
            is_best = prec1 > self.best_prec1
            if is_best:
                self.best_prec1 = prec1
                with open('record/rgb_video_preds.pickle','wb') as f:
                    pickle.dump(self.dic_video_level_preds,f)
                f.close() 
            
            save_checkpoint({
                'epoch': self.epoch,
                'state_dict': self.model.state_dict(),
                'best_prec1': self.best_prec1,
                'optimizer' : self.optimizer.state_dict()
            },is_best)
            
    def train_1epoch(self):
        batch_time = AverageMeter()
        data_time = AverageMeter()
        losses = AverageMeter()
        top1 = AverageMeter()
        top5 = AverageMeter()
        #switch to train mode
        self.model.train()    
        end = time.time()
        # mini-batch training
        for i, (data,label) in enumerate(tqdm(self.train_loader)):

    
            # measure data loading time
            data_time.update(time.time() - end)
            
            label = label.cuda(async=True)
            target_var = Variable(label).cuda()

           
            input_var = Variable(data).cuda()
            output = self.model(input_var)



            loss = self.criterion(output, target_var)

            # measure accuracy and record loss
            prec1, prec5 = accuracy(output.data, label, topk=(1, 5))
            losses.update(loss.data[0], data.size(0))
            top1.update(prec1[0], data.size(0))
            top5.update(prec5[0], data.size(0))

            # compute gradient and do SGD step
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()
    
        info = {'Epoch':[self.epoch],
                'Batch Time':[round(batch_time.avg,3)],
                'Data Time':[round(data_time.avg,3)],
                'Loss':[round(losses.avg,5)],
                'Prec@1':[round(top1.avg,4)],
                'Prec@5':[round(top5.avg,4)],
                'lr': [self.optimizer.param_groups[0]['lr']]
                }
        record_info(info, 'record/training.csv','train')

        return info

    def validate_1epoch(self):
        batch_time = AverageMeter()
        losses = AverageMeter()
        top1 = AverageMeter()
        top5 = AverageMeter()
        # switch to evaluate mode
        self.model.eval()
        self.dic_video_level_preds={}
        end = time.time()
        for i, (keys,data,label) in enumerate(tqdm(self.val_loader)):
            
            label = label.cuda(async=True)
            data_var = Variable(data, volatile=True).cuda(async=True)
            label_var = Variable(label, volatile=True).cuda(async=True)

            # compute output
            output = self.model(data_var)
            loss = self.criterion(output, label_var)

            # measure loss
            losses.update(loss.data[0], data.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()
            #Calculate video level prediction
            preds = output.data.cpu().numpy()
            nb_data = preds.shape[0]
            for j in range(nb_data):
                videoName = keys[j]
                if videoName not in self.dic_video_level_preds.keys():
                    self.dic_video_level_preds[videoName] = preds[j,:]
                else:
                    self.dic_video_level_preds[videoName] += preds[j,:]

        video_top1, video_top5, video_loss = self.frame2_video_level_accuracy()
        #print type(video_loss)

        info = {'Epoch':[self.epoch],
                'Batch Time':[round(batch_time.avg,3)],
                'Loss':[round(video_loss,5)],
                'Prec@1':[round(video_top1,3)],
                'Prec@5':[round(video_top5,3)]}
        record_info(info, 'record/testing.csv','test')

        return video_top1, video_loss, info

    def frame2_video_level_accuracy(self):
        with open('/home/ubuntu/cvlab/pytorch/Sub-JHMDB_pose_stream/get_train_test_split/dict/test_video.pickle','rb') as f:
            dic_video_label = pickle.load(f)
        f.close()
        print '==> validate on {} videos'.format(len(dic_video_label))
            
        correct = 0
        video_level_preds = np.zeros((len(self.dic_video_level_preds),12))
        video_level_labels = np.zeros(len(self.dic_video_level_preds))
        ii=0
        for key in sorted(self.dic_video_level_preds.keys()):
            name = key

            preds = self.dic_video_level_preds[name]
            label = int(dic_video_label[name])-1
                
            video_level_preds[ii,:] = preds
            video_level_labels[ii] = label
            ii+=1         
            if np.argmax(preds) == (label):
                correct+=1

        #top1 top5
        video_level_labels = torch.from_numpy(video_level_labels).long()
        video_level_preds = torch.from_numpy(video_level_preds).float()

        loss = self.criterion(Variable(video_level_preds).cuda(), Variable(video_level_labels).cuda())
            
        top1,top5 = accuracy(video_level_preds, video_level_labels, topk=(1,5))     
                            
        top1 = float(top1.numpy())
        top5 = float(top5.numpy())
            
        #print(' * Video level Prec@1 {top1:.3f}, Video level Prec@5 {top5:.3f}'.format(top1=top1, top5=top5))
        return top1,top5,loss.data.cpu().numpy()


if __name__ == '__main__':
    main()
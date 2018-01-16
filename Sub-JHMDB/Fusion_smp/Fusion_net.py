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

import rgb2d_resnet34 as rgb_net
import pose3d_resnet18 as pose_net
import opf2d_resenet50 as opf_net
import itertools
from util import *
from dataloader import *


os.environ["CUDA_VISIBLE_DEVICES"] = "3"

parser = argparse.ArgumentParser(description='PyTorch ResNet3D on Sub-JHMDB')
parser.add_argument('--epochs', default=500, type=int, metavar='N', help='number of total epochs')
parser.add_argument('--batch-size', default=16, type=int, metavar='N', help='mini-batch size (default: 64)')
parser.add_argument('--lr', default=1e-3, type=float, metavar='LR', help='initial learning rate')
parser.add_argument('--evaluate', dest='evaluate', action='store_true', help='evaluate model on validation set')
parser.add_argument('--resume', default='', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N', help='manual epoch number (useful on restarts)')

def main():
    global arg
    arg = parser.parse_args()
    print arg

    #Prepare DataLoader
    data_loader =Stack_joint_position_DataLoader(
                        BATCH_SIZE=arg.batch_size,
                        num_workers=8,
                        nb_per_stack=15,
                        dic_path='/home/ubuntu/cvlab/pytorch/Sub-JHMDB_pose_stream/get_train_test_split/dict/',
                        data_path='/home/ubuntu/data/JHMDB/pose_estimation/pose_estimation/',
                        anno_path='/home/ubuntu/data/JHMDB/bounding_box/'
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
        self.model = Fusion_net(
            RGB_weight='/home/ubuntu/cvlab/pytorch/Sub-JHMDB_pose_stream/REVICE_CODE/rgb_stream/55_resnet34/model_best.pth.tar',
            Pose_weight='/home/ubuntu/cvlab/pytorch/Sub-JHMDB_pose_stream/REVICE_CODE/3d_bbox_SJP/old_L15/model_best.pth.tar',
            Opf_weight = '/home/ubuntu/cvlab/pytorch/Sub-JHMDB_pose_stream/REVICE_CODE/opf+bbox/record/model_best.pth.tar'
        ).cuda()
        #print self.model
        #Loss function and optimizer
        self.criterion = nn.CrossEntropyLoss().cuda()
        parameter = itertools.chain(self.model.Rnet2.parameters(),self.model.fusion_Linear.parameters(),self.model.fusion_conv.parameters())
        self.optimizer = torch.optim.SGD(parameter, self.lr, momentum=0.9)
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
            R = Variable(data[0]).cuda()
            P = Variable(data[1]).cuda()
            O = Variable(data[2]).cuda()
            data_var = (R,P,O)

            output = self.model(data_var)
            loss = self.criterion(output, target_var)

            # measure accuracy and record loss
            prec1, prec5 = accuracy(output.data, label, topk=(1, 5))
            losses.update(loss.data[0], data[0].size(0))
            top1.update(prec1[0], data[0].size(0))
            top5.update(prec5[0], data[0].size(0))

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
            label_var = Variable(label, volatile=True).cuda(async=True)
            R = Variable(data[0]).cuda()
            P = Variable(data[1]).cuda()
            O = Variable(data[2]).cuda()
            data_var = (R,P,O)

            # compute output
            output = self.model(data_var)
            loss = self.criterion(output, label_var)

            # measure loss
            losses.update(loss.data[0], data[0].size(0))

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

class Fusion_net(nn.Module):
    def __init__(self, RGB_weight, Pose_weight,Opf_weight):
        super(Fusion_net, self).__init__()
        self.RGBnet = self.load_weight(rgb_net.resnet34(), RGB_weight) 
        self.Rnet1 = nn.Sequential(
                nn.AvgPool2d(2,padding=1),
            )
        self.Posenet = self.load_weight(pose_net.resnet18(), Pose_weight)

        self.OPFnet = self.load_weight(opf_net.resnet50(channel=20), Opf_weight)
        self.Rnet2 = nn.Sequential(
                nn.Conv2d(2048,1024,kernel_size=1, stride=1, bias=False),
                nn.Conv2d(1024,512,kernel_size=1, stride=1, bias=False),
                nn.AvgPool2d(2,padding=1),
            )        
        self.fusion_conv = nn.Sequential(
                nn.Conv2d(1536,768,kernel_size=1, stride=1, bias=False),
                nn.Conv2d(768,384,kernel_size=1, stride=1, bias=False),
                nn.Conv2d(384,192,kernel_size=1, stride=1, bias=False),
                nn.AvgPool2d(4)
            )
        self.fusion_Linear = nn.Sequential(
                nn.Dropout(0.5),
                nn.Linear(192,1024),
                nn.BatchNorm2d(1024),
                nn.ReLU(inplace=True),
                nn.Dropout(0.5),
                nn.Linear(1024,12)
        )

    def load_weight(self, model, weight_path):
        checkpoint = torch.load(weight_path)
        print("==> loaded checkpoint '{}' (epoch {}) (best_prec1 {})"
                  .format(weight_path, checkpoint['epoch'], checkpoint['best_prec1']))
        model_dict = checkpoint['state_dict']
        model.load_state_dict(model_dict)

        return model

    def forward(self, x):
        rx = self.RGBnet(x[0])
        px = self.Posenet(x[1])
        ox = self.OPFnet(x[2])

        #print rx.size(),px.size()
        ox = self.Rnet2(ox)
        rx = self.Rnet1(rx)        
        px =px.view(px.size(0),512,4,4)
        

        
        in_ = [rx, px, ox]  # merge the input of two stream
        x = torch.cat(in_, 1)
        x = self.fusion_conv(x)
        x = x.view(x.size(0),-1)
        x = self.fusion_Linear(x)

        return x




if __name__ == '__main__':
    main()
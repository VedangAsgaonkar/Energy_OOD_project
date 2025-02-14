# -*- coding: utf-8 -*-
# CUDA_VISIBLE_DEVICES=0 python3 train.py cifar10 --model wrn --score ranking --seed 1 --m_in -23 --m_out -5
import numpy as np
import os
import pickle
import argparse
import time
import torch
import torch.nn as nn
import torch.optim as optim
from collections import OrderedDict
import torch.backends.cudnn as cudnn
import torchvision.transforms as trn
import torchvision.datasets as dset
import torch.nn.functional as F
from tqdm import tqdm
from models.wrn import WideResNet, Model_20
from sklearn.datasets import fetch_20newsgroups
from keras.preprocessing.text import Tokenizer
from keras.utils import pad_sequences

if __package__ is None:
    import sys
    from os import path

    sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))
    from utils.tinyimages_80mn_loader import TinyImages
    from utils.validation_dataset import validation_split

parser = argparse.ArgumentParser(description='Tunes a CIFAR Classifier with OE',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--dataset', type=str, choices=['cifar10', 'cifar100', '20news'],
                    help='Choose between CIFAR-10, CIFAR-100 and 20news.')
parser.add_argument('--model', '-m', type=str, default='allconv',
                    choices=['allconv', 'wrn', 'densenet'], help='Choose architecture.')
parser.add_argument('--calibration', '-c', action='store_true',
                    help='Train a model to be used for calibration. This holds out some data for validation.')
# Optimization options
parser.add_argument('--epochs', '-e', type=int, default=10, help='Number of epochs to train.')
parser.add_argument('--learning_rate', '-lr', type=float, default=0.001, help='The initial learning rate.')
parser.add_argument('--batch_size', '-b', type=int, default=128, help='Batch size.')
parser.add_argument('--oe_batch_size', type=int, default=256, help='Batch size.')
parser.add_argument('--test_bs', type=int, default=200)
parser.add_argument('--momentum', type=float, default=0.9, help='Momentum.')
parser.add_argument('--decay', '-d', type=float, default=0.0005, help='Weight decay (L2 penalty).')
# WRN Architecture
parser.add_argument('--layers', default=40, type=int, help='total number of layers')
parser.add_argument('--widen-factor', default=2, type=int, help='widen factor')
parser.add_argument('--droprate', default=0.3, type=float, help='dropout probability')
# Checkpoints
parser.add_argument('--save', '-s', type=str, default='./snapshots/', help='Folder to save checkpoints.')
parser.add_argument('--load', '-l', type=str, default='./snapshots/pretrained', help='Checkpoint path to resume / test.')
parser.add_argument('--test', '-t', action='store_true', help='Test only flag.')
# Acceleration
parser.add_argument('--ngpu', type=int, default=1, help='0 = CPU.')
parser.add_argument('--prefetch', type=int, default=4, help='Pre-fetching threads.')
# EG specific
parser.add_argument('--m_in', type=float, default=-25., help='margin for in-distribution; above this value will be penalized')
parser.add_argument('--m_out', type=float, default=-7., help='margin for out-distribution; below this value will be penalized')
parser.add_argument('--margin',  type=float, default=20., help='margin for ranking loss')
parser.add_argument('--score', type=str, default='OE', help='OE|energy|ranking')
parser.add_argument('--seed', type=int, default=1, help='seed for np(tinyimages80M sampling); 1|2|8|100|107')
args = parser.parse_args()


if args.score == 'OE':
    save_info = 'oe_tune'
elif args.score == 'energy':
    save_info = 'energy_ft'
elif args.score == 'ranking':
    save_info = 'ranking'

args.save = args.save+save_info
if os.path.isdir(args.save) == False:
    os.mkdir(args.save)
state = {k: v for k, v in args._get_kwargs()}
print(state)

torch.manual_seed(1)
np.random.seed(args.seed)

# mean and standard deviation of channels of CIFAR-10 images
mean = [x / 255 for x in [125.3, 123.0, 113.9]]
std = [x / 255 for x in [63.0, 62.1, 66.7]]

train_transform = trn.Compose([trn.RandomHorizontalFlip(), trn.RandomCrop(32, padding=4),
                               trn.ToTensor(), trn.Normalize(mean, std)])
test_transform = trn.Compose([trn.ToTensor(), trn.Normalize(mean, std)])

if args.dataset == 'cifar10':
    train_data_in = dset.CIFAR10('../data/cifarpy', train=True, transform=train_transform)
    test_data = dset.CIFAR10('../data/cifarpy', train=False, transform=test_transform)
    num_classes = 10
    ood_data = dset.ImageFolder(root="../data/dtd/images",
                            transform=trn.Compose([trn.Resize(32), trn.CenterCrop(32), trn.RandomHorizontalFlip(),
                                                   trn.ToTensor(), trn.Normalize(mean, std)]))
elif args.dataset == 'cifar100':
    train_data_in = dset.CIFAR100('../data/cifarpy', train=True, transform=train_transform)
    test_data = dset.CIFAR100('../data/cifarpy', train=False, transform=test_transform)
    num_classes = 100
    ood_data = dset.ImageFolder(root="../data/dtd/images",
                            transform=trn.Compose([trn.Resize(32), trn.CenterCrop(32), trn.RandomHorizontalFlip(),
                                                   trn.ToTensor(), trn.Normalize(mean, std)]))
else :
    categories = ['alt.atheism',
                'comp.graphics',
                'comp.os.ms-windows.misc',
                'comp.sys.ibm.pc.hardware',
                'comp.sys.mac.hardware',
                'comp.windows.x',
                'misc.forsale',
                'rec.autos',
                'rec.motorcycles',
                'rec.sport.baseball',
                'rec.sport.hockey',
                'sci.crypt',
                'sci.electronics',
                'sci.med',
                'sci.space',
                'soc.religion.christian',
                'talk.politics.guns',
                'talk.politics.mideast',
                'talk.politics.misc',
                'talk.religion.misc']
    newsgroups_train = fetch_20newsgroups(subset='train', shuffle=True, categories=categories[:10])
    newsgroups_test = fetch_20newsgroups(subset='test', shuffle=True, categories=categories[:10])
    newsgroups_ood = fetch_20newsgroups(subset='train', shuffle=True, categories=categories[10:])

    MAX_SEQUENCE_LENGTH = 500
    MAX_NB_WORDS = 20000
    tokenizer = Tokenizer(num_words=MAX_NB_WORDS)
    # Preprocessing on train set......
    labels_train = newsgroups_train.target
    texts1 = newsgroups_train.data

    # tokenizer.fit_on_texts(texts)

    sequences = tokenizer.texts_to_sequences(texts1)
    train_data_in = pad_sequences(sequences, maxlen=MAX_SEQUENCE_LENGTH)
    
    # Preprocessing on test set
    labels_test = newsgroups_test.target
    texts2 = newsgroups_test.data

    # tokenizer.fit_on_texts(texts)
    sequences = tokenizer.texts_to_sequences(texts2)
    test_data = pad_sequences(sequences, maxlen=MAX_SEQUENCE_LENGTH)

    # Preprocessing on OOD data
    labels_ood = newsgroups_ood.target
    texts3 = newsgroups_ood.data

    # tokenizer.fit_on_texts(texts)
    sequences = tokenizer.texts_to_sequences(texts3)
    ood_data = pad_sequences(sequences, maxlen=MAX_SEQUENCE_LENGTH)

    texts1.extend(texts3)
    tokenizer.fit_on_texts(texts1)
    word_index = tokenizer.word_index


calib_indicator = ''
if args.calibration:
    train_data_in, val_data = validation_split(train_data_in, val_share=0.1)
    calib_indicator = '_calib'


# ood_data = TinyImages(transform=trn.Compose(
#     [trn.ToTensor(), trn.ToPILImage(), trn.RandomCrop(32, padding=4),
#      trn.RandomHorizontalFlip(), trn.ToTensor(), trn.Normalize(mean, std)]))


if args.dataset != '20news' :
    train_loader_in = torch.utils.data.DataLoader(
        train_data_in,
        batch_size=args.batch_size, shuffle=True,
        num_workers=args.prefetch, pin_memory=True)

    train_loader_out = torch.utils.data.DataLoader(
        ood_data,
        batch_size=args.oe_batch_size, shuffle=False,
        num_workers=args.prefetch, pin_memory=True)

    test_loader = torch.utils.data.DataLoader(
        test_data,
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.prefetch, pin_memory=True)

# Create model
if args.dataset == '20news' :
    embeddings_index = {}

    path = 'glove.6B/'

    f = open(path+'glove.6B.300d.txt')
    for line in f:
        values = line.split(' ')
        word = values[0]
        #values[-1] = values[-1].replace('\n', '')
        coefs = np.asarray(values[1:], dtype='float32')
        embeddings_index[word] = coefs
        #print (values[1:])
    f.close()

    EMBEDDING_DIM = 300

    embedding_matrix = np.random.random((len(word_index) + 1, EMBEDDING_DIM))
    print(f'after defining : {embedding_matrix.shape}')

    for word, i in word_index.items():
        embedding_vector = embeddings_index.get(word)
        #embedding_vector = embeddings_index[word]
        if embedding_vector is not None:
        # words not found in embedding index will be all-zeros.
            embedding_matrix[i] = embedding_vector

    net = Model_20(embedding_matrix.shape[0], EMBEDDING_DIM, embedding_matrix)
    # print(f'Net params : ({embedding_matrix.shape[0], EMBEDDING_DIM, embedding_matrix})')
else :
    net = WideResNet(args.layers, num_classes, args.widen_factor, dropRate=args.droprate)

def recursion_change_bn(module):
    if isinstance(module, torch.nn.BatchNorm2d):
        module.track_running_stats = 1
        module.num_batches_tracked = 0
    else:
        for i, (name, module1) in enumerate(module._modules.items()):
            module1 = recursion_change_bn(module1)
    return module
# Restore model
model_found = False
if args.load != 'nill':
    for i in range(1000 - 1, -1, -1):
        
        model_name = os.path.join(args.load, args.dataset + calib_indicator + '_' + args.model +
                                  '_pretrained_epoch_' + str(i) + '.pt')
        if os.path.isfile(model_name):
            net.load_state_dict(torch.load(model_name))
            print('Model restored! Epoch:', i)
            model_found = True
            break
    if not model_found:
        assert False, "could not find model to restore"

if args.ngpu > 1:
    net = torch.nn.DataParallel(net, device_ids=list(range(args.ngpu)))

if args.ngpu > 0:
    net.cuda()
    torch.cuda.manual_seed(1)

cudnn.benchmark = True  # fire on all cylinders

if args.dataset == '20news' :
    optimizer = optim.Adam(filter(lambda p: p.requires_grad, net.parameters()), lr=0.001)
else :
    optimizer = torch.optim.SGD(
        net.parameters(), state['learning_rate'], momentum=state['momentum'],
        weight_decay=state['decay'], nesterov=True)

    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lr_lambda=lambda step: cosine_annealing(
            step,
            args.epochs * len(train_loader_in),
            1,  # since lr_lambda computes multiplicative factor
            1e-6 / args.learning_rate))


def cosine_annealing(step, total_steps, lr_max, lr_min):
    return lr_min + (lr_max - lr_min) * 0.5 * (
            1 + np.cos(step / total_steps * np.pi))



# /////////////// Training ///////////////

if args.dataset == '20news' :
    def train() :
        net.train()
        loss_avg = 0.0
        print(f'Starting loop....')

        for in_set_tr, in_set_label, out_set_tr, out_set_label in zip(train_data_in, labels_train, ood_data, labels_ood) :
            data = np.concatenate((in_set_tr, out_set_tr), 0)
            target = np.zeros(10)
            target[int(in_set_label)] = 1
            # print(f'Initial target : {target}')
            # print(f'data and target taken.....')
            if torch.cuda.is_available() : data, target = data.cuda(), target.cuda()
            x = net(torch.LongTensor(data.reshape(1, -1)))

            optimizer.zero_grad()
            # print(f'Before cross entropy : ({x[:len(in_set_tr)].shape, torch.FloatTensor(target).shape})')
            loss = F.cross_entropy(x[:len(in_set_tr)], torch.FloatTensor(target))
            # cross-entropy from softmax distribution to uniform distribution
            if args.score == 'energy':
                Ec_out = -torch.logsumexp(x[len(in_set_tr):], dim=0)
                Ec_in = -torch.logsumexp(x[:len(in_set_tr)], dim=0)
                loss += 0.1*(torch.pow(F.relu(Ec_in-args.m_in), 2).mean() + torch.pow(F.relu(args.m_out-Ec_out), 2).mean())
            elif args.score == 'OE':
                loss += 0.5 * -(x[len(in_set_tr):].mean() - torch.logsumexp(x[len(in_set_tr):], dim=0)).mean()
            elif args.score == 'ranking':
                Ec_out = -torch.logsumexp(x[len(in_set_tr):], dim=0)
                Ec_in = -torch.logsumexp(x[:len(in_set_tr)], dim=0)
                loss += 0.1*torch.mean(torch.pow(args.margin-Ec_out[:,None]+Ec_in[None,:], 2))

            loss.backward()
            optimizer.step()

            print(f'loss_avg = {loss_avg}')
            loss_avg = loss_avg * 0.8 + float(loss) * 0.2
        state['train_loss'] = loss_avg
            
else :
    def train():
        net.train()  # enter train mode
        loss_avg = 0.0

        # start at a random point of the outlier dataset; this induces more randomness without obliterating locality
        train_loader_out.dataset.offset = np.random.randint(len(train_loader_out.dataset))
        for in_set, out_set in zip(train_loader_in, train_loader_out):
            data = torch.cat((in_set[0], out_set[0]), 0)
            target = in_set[1]

            if cuda.is_available() : data, target = data.cuda(), target.cuda()

            # forward
            x = net(data)

            # backward
            scheduler.step()
            optimizer.zero_grad()

            loss = F.cross_entropy(x[:len(in_set[0])], target)
            # cross-entropy from softmax distribution to uniform distribution
            if args.score == 'energy':
                Ec_out = -torch.logsumexp(x[len(in_set[0]):], dim=1)
                Ec_in = -torch.logsumexp(x[:len(in_set[0])], dim=1)
                loss += 0.1*(torch.pow(F.relu(Ec_in-args.m_in), 2).mean() + torch.pow(F.relu(args.m_out-Ec_out), 2).mean())
            elif args.score == 'OE':
                loss += 0.5 * -(x[len(in_set[0]):].mean(1) - torch.logsumexp(x[len(in_set[0]):], dim=1)).mean()
            elif args.score == 'ranking':
                Ec_out = -torch.logsumexp(x[len(in_set[0]):], dim=1)
                Ec_in = -torch.logsumexp(x[:len(in_set[0])], dim=1)
                loss += 0.1*torch.mean(torch.pow(args.margin-Ec_out[:,None]+Ec_in[None,:], 2))

            loss.backward()
            optimizer.step()

            # exponential moving average
            loss_avg = loss_avg * 0.8 + float(loss) * 0.2
        state['train_loss'] = loss_avg


# test function
def test():
    net.eval()
    loss_avg = 0.0
    correct = 0
    with torch.no_grad():
        if args.dataset == '20news' :
            for data, label in zip(test_data, labels_test) :
                if torch.cuda.is_available() : data, target = data.cuda(), target.cuda()

                target = np.zeros(10)
                target[int(label)] = 1

                output = net(torch.LongTensor(data.reshape(1, -1)))
                loss = F.cross_entropy(x[:len(in_set_tr)], torch.FloatTensor(target))

                pred = np.array(output.detach()).argmax()
                correct += int(pred == label)

                loss_avg += float(loss.data)
        else :
            for data, target in test_loader:
                if torch.cuda.is_available() : data, target = data.cuda(), target.cuda()

                # forward
                output = net(data)
                loss = F.cross_entropy(output, target)

                # accuracy
                pred = output.data.max(1)[1]
                correct += pred.eq(target.data).sum().item()

                # test loss average
                loss_avg += float(loss.data)

    state['test_loss'] = loss_avg / len(test_loader)
    state['test_accuracy'] = correct / len(test_loader.dataset)


if args.test:
    test()
    print(state)
    exit()

# Make save directory
if not os.path.exists(args.save):
    os.makedirs(args.save)
if not os.path.isdir(args.save):
    raise Exception('%s is not a dir' % args.save)

with open(os.path.join(args.save, args.dataset + calib_indicator + '_' + args.model + '_s' + str(args.seed) +
                                  '_' + save_info+'_training_results.csv'), 'w') as f:
    f.write('epoch,time(s),train_loss,test_loss,test_error(%)\n')

print('Beginning Training\n')

# Main loop
for epoch in range(0, args.epochs):
    state['epoch'] = epoch

    begin_epoch = time.time()

    train()
    test()
 
    # Save model
    torch.save(net.state_dict(),
               os.path.join(args.save, args.dataset + calib_indicator + '_' + args.model + '_s' + str(args.seed) +
                            '_' + save_info + '_epoch_' + str(epoch) + '.pt'))
               # Let us not waste space and delete the previous model
    prev_path = os.path.join(args.save, args.dataset + calib_indicator + '_' + args.model + '_s' + str(args.seed) +
                             '_' + save_info + '_epoch_'+ str(epoch - 1) + '.pt')
    if os.path.exists(prev_path): os.remove(prev_path)

    # Show results
    with open(os.path.join(args.save, args.dataset + calib_indicator + '_' + args.model + '_s' + str(args.seed) +
                                      '_' + save_info + '_training_results.csv'), 'a') as f:
        f.write('%03d,%05d,%0.6f,%0.5f,%0.2f\n' % (
            (epoch + 1),
            time.time() - begin_epoch,
            state['train_loss'],
            state['test_loss'],
            100 - 100. * state['test_accuracy'],
        ))

    # # print state with rounded decimals
    # print({k: round(v, 4) if isinstance(v, float) else v for k, v in state.items()})

    print('Epoch {0:3d} | Time {1:5d} | Train Loss {2:.4f} | Test Loss {3:.3f} | Test Error {4:.2f}'.format(
        (epoch + 1),
        int(time.time() - begin_epoch),
        state['train_loss'],
        state['test_loss'],
        100 - 100. * state['test_accuracy'])
    )

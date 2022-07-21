import logging
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parallel import DataParallel, DistributedDataParallel
import numpy as np
import models.networks as networks
import models.lr_scheduler as lr_scheduler
from .base_model import BaseModel
from models.loss import CharbonnierLoss

logger = logging.getLogger("base")


class VideoColorizationModel(BaseModel):
    def __init__(self, opt):
        super(VideoColorizationModel, self).__init__(opt)

        if opt["dist"]:
            self.rank = torch.distributed.get_rank()
        else:
            self.rank = -1  # non dist training
        train_opt = opt["train"]

        # define network and load pretrained models
        self.netG = networks.define_G(opt).to(self.device)
        if opt["dist"]:
            self.netG = DistributedDataParallel(
                self.netG, device_ids=[torch.cuda.current_device()]
            )
        else:
            self.netG = DataParallel(self.netG)
        # print network
        self.print_network()
        self.load()

        if self.is_train:
            self.netG.train()

            # loss
            loss_type = train_opt["pixel_criterion"]
            self.MSE_loss = nn.MSELoss().to(self.device)
            #self.MSE_loss = nn.L1Loss().to(self.device)
            
            self.loss_type = loss_type
            if loss_type == "l1":
                self.cri_pix = nn.L1Loss().to(self.device)
            elif loss_type == "l2":
                self.cri_pix = nn.MSELoss().to(self.device)
            elif loss_type == "SmoothL1Loss":
                self.cri_pix = nn.SmoothL1Loss().to(self.device)
            elif loss_type == "cb":
                self.cri_pix = CharbonnierLoss().to(self.device)
            else:
                raise NotImplementedError(
                    "Loss type [{:s}] is not recognized.".format(loss_type)
                )
            self.l_pix_w = train_opt["pixel_weight"]
            if train_opt["grad_weight"] != None:
                self.l_grad_w = train_opt["grad_weight"]
            if train_opt["fs_weight"] != None:
                self.l_fs_w = train_opt["fs_weight"]

            # optimizers
            wd_G = train_opt["weight_decay_G"] if train_opt["weight_decay_G"] else 0
            optim_params = []
            for (
                k,
                v,
            ) in self.netG.named_parameters():  # can optimize for a part of the model
                if v.requires_grad:
                    optim_params.append(v)
                else:
                    if self.rank <= 0:
                        logger.warning("Params [{:s}] will not optimize.".format(k))
            self.optimizer_G = torch.optim.Adam(
                optim_params,
                lr=train_opt["lr_G"],
                weight_decay=wd_G,
                betas=(train_opt["beta1"], train_opt["beta2"]),
            )
            self.optimizers.append(self.optimizer_G)

            # schedulers
            if train_opt["lr_scheme"] == "MultiStepLR":
                for optimizer in self.optimizers:
                    self.schedulers.append(
                        lr_scheduler.MultiStepLR_Restart(
                            optimizer,
                            train_opt["lr_steps"],
                            restarts=train_opt["restarts"],
                            weights=train_opt["restart_weights"],
                            gamma=train_opt["lr_gamma"],
                            clear_state=train_opt["clear_state"],
                        )
                    )
            elif train_opt["lr_scheme"] == "CosineAnnealingLR_Restart":
                for optimizer in self.optimizers:
                    self.schedulers.append(
                        lr_scheduler.CosineAnnealingLR_Restart(
                            optimizer,
                            train_opt["T_period"],
                            eta_min=train_opt["eta_min"],
                            restarts=train_opt["restarts"],
                            weights=train_opt["restart_weights"],
                        )
                    )
            else:
                raise NotImplementedError("MultiStepLR learning rate scheme is enough.")

            self.log_dict = OrderedDict()

    def feed_data(self, data, need_GT=True):
        self.var_L = [v.to(self.device) for v in data["LQs"]]
        self.GT_HW = data["GT_HW"]
        if need_GT:
            self.real_Lab = data["GT"].to(self.device)

    def optimize_parameters(self, step):
        self.optimizer_G.zero_grad()
        total_loss = 0

        self.fake_ab, self.ab_fwarp_l, self.non_mask_fwarp_l, self.ab_fwarp_l_2, self.non_mask_fwarp_l_2  = self.netG(self.var_L)
        l_pix = self.l_pix_w * self.MSE_loss(self.fake_ab, self.real_Lab[:, :, 1:, :, :])
        
        #self.MSE_loss(self.fake_ab[:,] * noc_mask2, warp_o1 * noc_mask2)
        
        #print('out_l: ', self.fake_ab.size()) # torch.Size([1, 3, 2, 256, 256])
        #print('ab_warp: ', self.ab_fwarp_l.size()) # torch.Size([1, 2, 2, 256, 256])
        #print('non_mask: ', self.non_mask_fwarp_l.size()) # torch.Size([1, 2, 1, 256, 256])
        warp_loss = self.MSE_loss(self.non_mask_fwarp_l*self.fake_ab[:,1:,:,:], self.non_mask_fwarp_l*self.ab_fwarp_l)
        warp_loss_2 = self.MSE_loss(self.non_mask_fwarp_l_2*self.fake_ab[:,2:,:,:], self.non_mask_fwarp_l_2*self.ab_fwarp_l_2)
        
        total_loss = l_pix + warp_loss + warp_loss_2
        total_loss.backward()
        self.optimizer_G.step()

        # set log
        self.log_dict["l_pix"] = l_pix.item()
        self.log_dict["warp_loss"] = warp_loss.item()
        self.log_dict["warp_loss2"] = warp_loss_2.item()

    def test(self):
        self.netG.eval()
        with torch.no_grad():
            fake_ab = self.netG(self.var_L)
            fake_ab = F.interpolate(
                fake_ab[:, 0, ...], size=self.GT_HW, mode="bilinear"
            )
            self.fake_Lab = torch.cat((self.real_Lab[:, :1, :, :], fake_ab), 1)

        self.netG.train()

    def test_x8(self):
        # from https://github.com/thstkdgus35/EDSR-PyTorch
        self.netG.eval()

        def _transform(v, op):
            # if self.precision != 'single': v = v.float()
            v2np = v.data.cpu().numpy()
            if op == "v":
                tfnp = v2np[:, :, :, ::-1].copy()
            elif op == "h":
                tfnp = v2np[:, :, ::-1, :].copy()
            elif op == "t":
                tfnp = v2np.transpose((0, 1, 3, 2)).copy()

            ret = torch.Tensor(tfnp).to(self.device)
            # if self.precision == 'half': ret = ret.half()

            return ret

        lr_list = [self.var_L]
        for tf in "v", "h", "t":
            lr_list.extend([_transform(t, tf) for t in lr_list])
        with torch.no_grad():
            sr_list = [self.netG(aug) for aug in lr_list]
        for i in range(len(sr_list)):
            if i > 3:
                sr_list[i] = _transform(sr_list[i], "t")
            if i % 4 > 1:
                sr_list[i] = _transform(sr_list[i], "h")
            if (i % 4) % 2 == 1:
                sr_list[i] = _transform(sr_list[i], "v")

        output_cat = torch.cat(sr_list, dim=0)
        self.fake_H = output_cat.mean(dim=0, keepdim=True)
        self.netG.train()

    def get_current_log(self):
        return self.log_dict

    def get_current_visuals(self, need_GT=True):
        out_dict = OrderedDict()
        out_dict["LQ"] = [v.detach()[0].float().cpu() for v in self.var_L]
        out_dict["rlt"] = self.fake_Lab.detach()[0].float().cpu()
        if need_GT:
            out_dict["GT"] = self.real_Lab.detach()[0].float().cpu()
        return out_dict

    def print_network(self):
        s, n = self.get_network_description(self.netG)
        if isinstance(self.netG, nn.DataParallel) or isinstance(
            self.netG, DistributedDataParallel
        ):
            net_struc_str = "{} - {}".format(
                self.netG.__class__.__name__, self.netG.module.__class__.__name__
            )
        else:
            net_struc_str = "{}".format(self.netG.__class__.__name__)
        if self.rank <= 0:
            logger.info(
                "Network G structure: {}, with parameters: {:,d}".format(
                    net_struc_str, n
                )
            )
            logger.info(s)

    def load(self):
        load_path_G = self.opt["path"]["pretrain_model_G"]
        if load_path_G is not None:
            logger.info("Loading model for G [{:s}] ...".format(load_path_G))
            self.load_network(load_path_G, self.netG, self.opt["path"]["strict_load"])

    def save(self, iter_label):
        self.save_network(self.netG, "G", iter_label)

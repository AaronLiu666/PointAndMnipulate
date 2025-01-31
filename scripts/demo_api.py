# -----------------------------------------------------
# Copyright (c) Shanghai Jiao Tong University. All rights reserved.
# Written by Haoyi Zhu,Hao-Shu Fang
# -----------------------------------------------------

"""Script for single-image demo."""
import argparse
import torch
import os
import platform
import sys
import math
import time
import pyrealsense2 as rs
import rospy
from std_msgs.msg import Float32MultiArray

import cv2
import numpy as np

from alphapose.utils.transforms import get_func_heatmap_to_coord
from alphapose.utils.pPose_nms import pose_nms
from alphapose.utils.presets import SimpleTransform, SimpleTransform3DSMPL
from alphapose.utils.transforms import flip, flip_heatmap
from alphapose.models import builder
from alphapose.utils.config import update_config
from detector.apis import get_detector
from alphapose.utils.vis import getTime
# from scripts.twoD2threeD import get_3d_camera_coordinate, get_aligned_images

"""----------------------------- Demo options -----------------------------"""
parser = argparse.ArgumentParser(description='AlphaPose Single-Image Demo')
parser.add_argument('--cfg', type=str, default='configs/halpe_coco_wholebody_136/resnet/256x192_res50_lr1e-3_2x-dcn-combined.yaml',
                    help='experiment configure file name')
parser.add_argument('--checkpoint', type=str, default='pretrained_models/multi_domain_fast50_dcn_combined_256x192.pth',
                    help='checkpoint file name')
parser.add_argument('--detector', dest='detector',
                    help='detector name', default="yolo")
parser.add_argument('--image', dest='inputimg',
                    help='image-name', default="")
parser.add_argument('--save_img', default=False, action='store_true',
                    help='save result as image')
parser.add_argument('--vis', default=False, action='store_true',
                    help='visualize image')
parser.add_argument('--showbox', default=False, action='store_true',
                    help='visualize human bbox')
parser.add_argument('--profile', default=False, action='store_true',
                    help='add speed profiling at screen output')
parser.add_argument('--format', type=str,
                    help='save in the format of cmu or coco or openpose, option: coco/cmu/open')
parser.add_argument('--min_box_area', type=int, default=0,
                    help='min box area to filter out')
parser.add_argument('--eval', dest='eval', default=False, action='store_true',
                    help='save the result json as coco format, using image index(int) instead of image name(str)')
parser.add_argument('--gpus', type=str, dest='gpus', default="0",
                    help='choose which cuda device to use by index and input comma to use multi gpus, e.g. 0,1,2,3. (input -1 for cpu only)')
parser.add_argument('--flip', default=False, action='store_true',
                    help='enable flip testing')
parser.add_argument('--debug', default=False, action='store_true',
                    help='print detail information')
parser.add_argument('--vis_fast', dest='vis_fast',
                    help='use fast rendering', action='store_true', default=False)
"""----------------------------- Tracking options -----------------------------"""
parser.add_argument('--pose_flow', dest='pose_flow',
                    help='track humans in video with PoseFlow', action='store_true', default=False)
parser.add_argument('--pose_track', dest='pose_track',
                    help='track humans in video with reid', action='store_true', default=False)

args = parser.parse_args()
cfg = update_config(args.cfg)

args.gpus = [int(args.gpus[0])] if torch.cuda.device_count() >= 1 else [-1]
args.device = torch.device("cuda:" + str(args.gpus[0]) if args.gpus[0] >= 0 else "cpu")
args.tracking = args.pose_track or args.pose_flow or args.detector=='tracker'

class DetectionLoader():
    def __init__(self, detector, cfg, opt):
        self.cfg = cfg
        self.opt = opt
        self.device = opt.device
        self.detector = detector

        self._input_size = cfg.DATA_PRESET.IMAGE_SIZE
        self._output_size = cfg.DATA_PRESET.HEATMAP_SIZE

        self._sigma = cfg.DATA_PRESET.SIGMA

        if cfg.DATA_PRESET.TYPE == 'simple':
            pose_dataset = builder.retrieve_dataset(self.cfg.DATASET.TRAIN)
            self.transformation = SimpleTransform(
                pose_dataset, scale_factor=0,
                input_size=self._input_size,
                output_size=self._output_size,
                rot=0, sigma=self._sigma,
                train=False, add_dpg=False, gpu_device=self.device)
        elif cfg.DATA_PRESET.TYPE == 'simple_smpl':
            # TODO: new features
            from easydict import EasyDict as edict
            dummpy_set = edict({
                'joint_pairs_17': None,
                'joint_pairs_24': None,
                'joint_pairs_29': None,
                'bbox_3d_shape': (2.2, 2.2, 2.2)
            })
            self.transformation = SimpleTransform3DSMPL(
                dummpy_set, scale_factor=cfg.DATASET.SCALE_FACTOR,
                color_factor=cfg.DATASET.COLOR_FACTOR,
                occlusion=cfg.DATASET.OCCLUSION,
                input_size=cfg.MODEL.IMAGE_SIZE,
                output_size=cfg.MODEL.HEATMAP_SIZE,
                depth_dim=cfg.MODEL.EXTRA.DEPTH_DIM,
                bbox_3d_shape=(2.2, 2,2, 2.2),
                rot=cfg.DATASET.ROT_FACTOR, sigma=cfg.MODEL.EXTRA.SIGMA,
                train=False, add_dpg=False, gpu_device=self.device,
                loss_type=cfg.LOSS['TYPE'])

        self.image = (None, None, None, None)
        self.det = (None, None, None, None, None, None, None)
        self.pose = (None, None, None, None, None, None, None)

    def process(self, im_name, image):
        # start to pre process images for object detection
        self.image_preprocess(im_name, image)
        # start to detect human in images
        self.image_detection()
        # start to post process cropped human image for pose estimation
        self.image_postprocess()
        return self

    def image_preprocess(self, im_name, image):
        # expected image shape like (1,3,h,w) or (3,h,w)
        img = self.detector.image_preprocess(image)
        if isinstance(img, np.ndarray):
            img = torch.from_numpy(img)
        # add one dimension at the front for batch if image shape (3,h,w)
        if img.dim() == 3:
            img = img.unsqueeze(0)
        orig_img = image # scipy.misc.imread(im_name_k, mode='RGB') is depreciated
        im_dim = orig_img.shape[1], orig_img.shape[0]

        im_name = os.path.basename(im_name)
        # im_name = 'test.jpeg'

        with torch.no_grad():
            im_dim = torch.FloatTensor(im_dim).repeat(1, 2)

        self.image = (img, orig_img, im_name, im_dim)

    def image_detection(self):
        imgs, orig_imgs, im_names, im_dim_list = self.image
        if imgs is None:
            self.det = (None, None, None, None, None, None, None)
            return

        with torch.no_grad():
            dets = self.detector.images_detection(imgs, im_dim_list)
            if isinstance(dets, int) or dets.shape[0] == 0:
                self.det = (orig_imgs, im_names, None, None, None, None, None)
                return
            if isinstance(dets, np.ndarray):
                dets = torch.from_numpy(dets)
            dets = dets.cpu()
            boxes = dets[:, 1:5]
            scores = dets[:, 5:6]
            ids = torch.zeros(scores.shape)

        boxes = boxes[dets[:, 0] == 0]
        if isinstance(boxes, int) or boxes.shape[0] == 0:
            self.det = (orig_imgs, im_names, None, None, None, None, None)
            return
        inps = torch.zeros(boxes.size(0), 3, *self._input_size)
        cropped_boxes = torch.zeros(boxes.size(0), 4)

        self.det = (orig_imgs, im_names, boxes, scores[dets[:, 0] == 0], ids[dets[:, 0] == 0], inps, cropped_boxes)

    def image_postprocess(self):
        with torch.no_grad():
            (orig_img, im_name, boxes, scores, ids, inps, cropped_boxes) = self.det
            if orig_img is None:
                self.pose = (None, None, None, None, None, None, None)
                return
            if boxes is None or boxes.nelement() == 0:
                self.pose = (None, orig_img, im_name, boxes, scores, ids, None)
                return

            for i, box in enumerate(boxes):
                inps[i], cropped_box = self.transformation.test_transform(orig_img, box)
                cropped_boxes[i] = torch.FloatTensor(cropped_box)

            self.pose = (inps, orig_img, im_name, boxes, scores, ids, cropped_boxes)

    def read(self):
        return self.pose


class DataWriter():
    def __init__(self, cfg, opt):
        self.cfg = cfg
        self.opt = opt

        self.eval_joints = list(range(cfg.DATA_PRESET.NUM_JOINTS))
        self.heatmap_to_coord = get_func_heatmap_to_coord(cfg)
        self.item = (None, None, None, None, None, None, None)
        
        loss_type = self.cfg.DATA_PRESET.get('LOSS_TYPE', 'MSELoss')
        num_joints = self.cfg.DATA_PRESET.NUM_JOINTS
        if loss_type == 'MSELoss':
            self.vis_thres = [0.4] * num_joints
        elif 'JointRegression' in loss_type:
            self.vis_thres = [0.05] * num_joints
        elif loss_type == 'Combined':
            if num_joints == 68:
                hand_face_num = 42
            else:
                hand_face_num = 110
            self.vis_thres = [0.4] * (num_joints - hand_face_num) + [0.05] * hand_face_num

        self.use_heatmap_loss = (self.cfg.DATA_PRESET.get('LOSS_TYPE', 'MSELoss') == 'MSELoss')

    def start(self):
        # start to read pose estimation results
        return self.update()

    def update(self):
        norm_type = self.cfg.LOSS.get('NORM_TYPE', None)
        hm_size = self.cfg.DATA_PRESET.HEATMAP_SIZE

        # get item
        (boxes, scores, ids, hm_data, cropped_boxes, orig_img, im_name) = self.item
        if orig_img is None:
            return None
        # image channel RGB->BGR
        orig_img = np.array(orig_img, dtype=np.uint8)[:, :, ::-1]
        self.orig_img = orig_img
        if boxes is None or len(boxes) == 0:
            return None
        else:
            # location prediction (n, kp, 2) | score prediction (n, kp, 1)
            assert hm_data.dim() == 4
            if hm_data.size()[1] == 136:
                self.eval_joints = [*range(0,136)]
            elif hm_data.size()[1] == 26:
                self.eval_joints = [*range(0,26)]
            elif hm_data.size()[1] == 133:
                self.eval_joints = [*range(0,133)]
            pose_coords = []
            pose_scores = []

            for i in range(hm_data.shape[0]):
                bbox = cropped_boxes[i].tolist()
                if isinstance(self.heatmap_to_coord, list):
                    pose_coords_body_foot, pose_scores_body_foot = self.heatmap_to_coord[0](
                        hm_data[i][self.eval_joints[:-110]], bbox, hm_shape=hm_size, norm_type=norm_type)
                    pose_coords_face_hand, pose_scores_face_hand = self.heatmap_to_coord[1](
                        hm_data[i][self.eval_joints[-110:]], bbox, hm_shape=hm_size, norm_type=norm_type)
                    pose_coord = np.concatenate((pose_coords_body_foot, pose_coords_face_hand), axis=0)
                    pose_score = np.concatenate((pose_scores_body_foot, pose_scores_face_hand), axis=0)
                else:
                    pose_coord, pose_score = self.heatmap_to_coord(hm_data[i][self.eval_joints], bbox, hm_shape=hm_size, norm_type=norm_type)
                pose_coords.append(torch.from_numpy(pose_coord).unsqueeze(0))
                pose_scores.append(torch.from_numpy(pose_score).unsqueeze(0))
            preds_img = torch.cat(pose_coords)
            preds_scores = torch.cat(pose_scores)

            boxes, scores, ids, preds_img, preds_scores, pick_ids = \
                pose_nms(boxes, scores, ids, preds_img, preds_scores, self.opt.min_box_area, use_heatmap_loss=self.use_heatmap_loss)

            _result = []
            for k in range(len(scores)):
                _result.append(
                    {
                        'keypoints':preds_img[k],
                        'kp_score':preds_scores[k],
                        'proposal_score': torch.mean(preds_scores[k]) + scores[k] + 1.25 * max(preds_scores[k]),
                        'idx':ids[k],
                        'bbox':[boxes[k][0], boxes[k][1], boxes[k][2]-boxes[k][0],boxes[k][3]-boxes[k][1]] 
                    }
                )

            result = {
                'imgname': im_name,
                'result': _result
            }

            if hm_data.size()[1] == 49:
                from alphapose.utils.vis import vis_frame_dense as vis_frame
            elif self.opt.vis_fast:
                from alphapose.utils.vis import vis_frame_fast as vis_frame
            else:
                from alphapose.utils.vis import vis_frame
            self.vis_frame = vis_frame

        return result

    def save(self, boxes, scores, ids, hm_data, cropped_boxes, orig_img, im_name):
        self.item = (boxes, scores, ids, hm_data, cropped_boxes, orig_img, im_name)

class SingleImageAlphaPose():
    def __init__(self, args, cfg):
        self.args = args
        self.cfg = cfg

        # Load pose model
        self.pose_model = builder.build_sppe(cfg.MODEL, preset_cfg=cfg.DATA_PRESET)

        print(f'Loading pose model from {args.checkpoint}...')
        self.pose_model.load_state_dict(torch.load(args.checkpoint, map_location=args.device))
        self.pose_dataset = builder.retrieve_dataset(cfg.DATASET.TRAIN)

        self.pose_model.to(args.device)
        self.pose_model.eval()
        
        self.det_loader = DetectionLoader(get_detector(self.args), self.cfg, self.args)

    def process(self, im_name, image):
        # Init data writer
        self.writer = DataWriter(self.cfg, self.args)

        runtime_profile = {
            'dt': [],
            'pt': [],
            'pn': []
        }
        pose = None
        try:
            start_time = getTime()
            with torch.no_grad():
                (inps, orig_img, im_name, boxes, scores, ids, cropped_boxes) = self.det_loader.process(im_name, image).read()
                if orig_img is None:
                    raise Exception("no image is given")
                if boxes is None or boxes.nelement() == 0:
                    if self.args.profile:
                        ckpt_time, det_time = getTime(start_time)
                        runtime_profile['dt'].append(det_time)
                    self.writer.save(None, None, None, None, None, orig_img, im_name)
                    if self.args.profile:
                        ckpt_time, pose_time = getTime(ckpt_time)
                        runtime_profile['pt'].append(pose_time)
                    pose = self.writer.start()
                    if self.args.profile:
                        ckpt_time, post_time = getTime(ckpt_time)
                        runtime_profile['pn'].append(post_time)
                else:
                    if self.args.profile:
                        ckpt_time, det_time = getTime(start_time)
                        runtime_profile['dt'].append(det_time)
                    # Pose Estimation
                    inps = inps.to(self.args.device)
                    if self.args.flip:
                        inps = torch.cat((inps, flip(inps)))
                    hm = self.pose_model(inps)
                    if self.args.flip:
                        hm_flip = flip_heatmap(hm[int(len(hm) / 2):], self.pose_dataset.joint_pairs, shift=True)
                        hm = (hm[0:int(len(hm) / 2)] + hm_flip) / 2
                    if self.args.profile:
                        ckpt_time, pose_time = getTime(ckpt_time)
                        runtime_profile['pt'].append(pose_time)
                    hm = hm.cpu()
                    self.writer.save(boxes, scores, ids, hm, cropped_boxes, orig_img, im_name)
                    pose = self.writer.start()
                    if self.args.profile:
                        ckpt_time, post_time = getTime(ckpt_time)
                        runtime_profile['pn'].append(post_time)

            if self.args.profile:
                print(
                    'det time: {dt:.4f} | pose time: {pt:.4f} | post processing: {pn:.4f}'.format(
                        dt=np.mean(runtime_profile['dt']), pt=np.mean(runtime_profile['pt']), pn=np.mean(runtime_profile['pn']))
                )
            # print('===========================> Finish Model Running.')
        except Exception as e:
            print(repr(e))
            print('An error as above occurs when processing the images, please check it')
            pass
        except KeyboardInterrupt:
            print('===========================> Finish Model Running.')

        return pose

    def getImg(self):
        return self.writer.orig_img

    def vis(self, image, pose):
        if pose is not None:
            image = self.writer.vis_frame(image, pose, self.writer.opt, self.writer.vis_thres)
        return image

    def writeJson(self, final_result, outputpath, form='coco', for_eval=False):
        from alphapose.utils.pPose_nms import write_json
        write_json(final_result, outputpath, form=form, for_eval=for_eval)
        print("Results have been written to json.")

def example():
    outputpath = "examples/res/"
    if not os.path.exists(outputpath + '/vis'):
        os.mkdir(outputpath + '/vis')

    demo = SingleImageAlphaPose(args, cfg)
    im_name = args.inputimg    # the path to the target image
    image = cv2.cvtColor(cv2.imread(im_name), cv2.COLOR_BGR2RGB)
    pose = demo.process('another.jpeg', image)
    img = demo.getImg()     # or you can just use: img = cv2.imread(image)
    img = demo.vis(img, pose)   # visulize the pose result
    cv2.imwrite(os.path.join(outputpath, 'vis', os.path.basename(im_name)), img)
    
    # if you want to vis the img:
    # cv2.imshow("AlphaPose Demo", img)
    # cv2.waitKey(30)

    # write the result to json:
    result = [pose]
    print(type(pose))
    demo.writeJson(result, outputpath, form=args.format, for_eval=args.eval)
    
def get_needed_points(kp):
    # select needed keypoints from all kp
    if torch.is_tensor(kp):
        kpn = kp.numpy()
    elif type(kp) == list or type(kp) == tuple:
        kpn = np.array(kp)
    else:
        print('Not acceptable type.')
        return
    
    LE, RE, LW, RW = kpn[7:11,:]
    kp_need = kpn[7:11,:]
    return kp_need

def get_aligned_images(align, pipeline):
    
    frames = pipeline.wait_for_frames()     
    aligned_frames = align.process(frames)      

    aligned_depth_frame = aligned_frames.get_depth_frame()      
    aligned_color_frame = aligned_frames.get_color_frame()      

    #### 获取相机参数 ####
    depth_intrin = aligned_depth_frame.profile.as_video_stream_profile().intrinsics     
    # color_intrin = aligned_color_frame.profile.as_video_stream_profile().intrinsics     


    #### 将images转为numpy arrays ####  
    img_color = np.asanyarray(aligned_color_frame.get_data())       # RGB图  
    img_depth = np.asanyarray(aligned_depth_frame.get_data())       # 深度图（默认16位）

    return depth_intrin, img_color, img_depth, aligned_depth_frame

def visualize(points, color_img):
    radius = 3
    color = (0, 0, 255) # BGR format
    thickness = -1 # Negative thickness fills the circle
    for point in points:
        cv2.circle(color_img, point, radius, color, thickness)

def get_3d_camera_coordinate(points, aligned_depth_frame, depth_intrin):
    camera_coordinates = []
    for point in points:
        if point[0]>640: point[0]=640 
        elif point[0]<2: point[0]=1
        if point[1]>480: point[1] = 480 
        elif point[1]<2: point[1]=1
        camera_coordinates.append(rs.rs2_deproject_pixel_to_point(depth_intrin, point, aligned_depth_frame.get_distance(int(point[0]-1), int(point[1]-1))))
        # camera_coordinates.append(rs.rs2_deproject_pixel_to_point(depth_intrin, point, aligned_depth_frame.get_distance(0,0)))
    return camera_coordinates

def transform_coordinate(coord, transform_matrix):
    """
    将3x1坐标向量应用4x4变换矩阵，返回转换后的3x1坐标向量
    :param coord: 3x1坐标向量
    :param transform_matrix: 4x4变换矩阵
    :return: 转换后的3x1坐标向量
    """
    # 将3x1坐标向量扩展为4x1齐次坐标
    coord_homogeneous = np.vstack((coord, 1))

    # 应用变换矩阵
    transformed_coord_homogeneous = np.dot(transform_matrix, coord_homogeneous)

    # 将齐次坐标还原为3x1坐标向量
    transformed_coord = transformed_coord_homogeneous[:3] / transformed_coord_homogeneous[3]

    return transformed_coord

def transform_coordinates(coords, transform_matrix):
    """
    将二维坐标数组应用4x4变换矩阵，返回转换后的坐标数组
    :param coords: n*2的坐标数组
    :param transform_matrix: 4x4变换矩阵
    :return: 转换后的坐标数组
    """
    # 将二维坐标数组扩展为n*3的齐次坐标数组
    coords_homogeneous = np.hstack((coords, np.ones((len(coords), 1))))

    # 应用变换矩阵
    transformed_coords_homogeneous = np.dot(transform_matrix, coords_homogeneous.T).T

    # 将齐次坐标还原为二维坐标
    transformed_coords = transformed_coords_homogeneous[:, :-1] / transformed_coords_homogeneous[:, -1].reshape(-1, 1)

    return transformed_coords

def generate_transform_matrix(coords_after, translation):
    """
    根据变换后坐标系的单位向量在原坐标系下的表示和平移向量，生成4x4的变换矩阵
    :param coords_after: 变换后坐标系单位向量在原坐标系下的表示，3x3的旋转矩阵
    :param translation: 平移向量，4x1的列向量，其中第4个元素为1
    :return: 4x4的变换矩阵
    """
    # 构造变换矩阵
    transform_matrix = np.vstack((np.hstack((coords_after, translation[:3])), np.array([0, 0, 0, 1])))

    return transform_matrix
    
def js():
    pipeline = rs.pipeline()
    config = rs.config()
    config.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, 30)
    config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
    pipeline.start(config)
    align_to = rs.stream.color      
    align = rs.align(align_to)
    x_u = [1,0,0]
    y_u = [0,0,1]
    z_u = [0,-1,0]
    trans = [[0],[0],[1.32],[1]]
    
    transform_matrix = generate_transform_matrix([x_u,y_u,z_u],trans)
    
    
    
    demo = SingleImageAlphaPose(args, cfg)
    im_name = 'ljs_img.jpeg'
    rospy.init_node('ljs')
    coord_pub = rospy.Publisher('/coords', Float32MultiArray, queue_size=10)
    rate = rospy.Rate(10)
    

    try:
        while True:
            
            depth_intrin, img_color, _, aligned_depth_frame = get_aligned_images(align, pipeline)        # 获取对齐图像与相机参数
            depth_pixel = [320, 240]        
            

            color_img = np.asanyarray(img_color)
            image = cv2.cvtColor(color_img, cv2.COLOR_BGR2RGB)
            pose = demo.process(im_name, image)
            cv2.imshow('color', image)
            # print(pose)
            if pose is not None:
                res = pose['result']
                keypoint = res[0] # choose the first people recognized, may changing if multiple detected
                # print(len(res)) # number of people recognized
                keypoint = keypoint['keypoints']
                # print(type(keypoint)) # tensor
                # kp = keypoint.numpy()
                # print(type(kp)) # ndarray
                # print(keypoint.shape)
                # print(keypoint[0:5,:])
                # print(kp.shape) # (136,2) coordinates of 136 keypoints
                kp = get_needed_points(keypoint)
                # print(kp)
                camera_coordinates = get_3d_camera_coordinate(kp, aligned_depth_frame, depth_intrin)
                world_coordinates = transform_coordinates(camera_coordinates, transform_matrix)
                # print(world_coordinates)
                coord_msg = Float32MultiArray()
                coord_msg.data = world_coordinates.flatten().tolist()
                coord_pub.publish(coord_msg)
                
                
                # print(world_coordinates)
                visualize(kp.astype(int), image)
                
                cv2.imshow('color', image)
                rate.sleep()


                # end program
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
            
    finally:
        # cleanup
        pipeline.stop()
        cv2.destroyAllWindows()
    
        
        


if __name__ == "__main__":
    # example()
    js()

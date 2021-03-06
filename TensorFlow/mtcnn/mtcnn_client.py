import time
import cv2
import numpy as np
from scipy import misc
from io import BytesIO
from PIL import Image
import sys
import os
import argparse

import tensorflow as tf
from grpc.beta import implementations
from tensorflow.python.framework.tensor_util import MakeNdarray
from tensorflow_serving.apis import predict_pb2
from tensorflow_serving.apis import prediction_service_pb2

from tensorflow.core.framework import tensor_pb2
from tensorflow.core.framework import tensor_shape_pb2
from tensorflow.core.framework import types_pb2

def __imresample(img, sz):
    return cv2.resize(img, (sz[1], sz[0]), interpolation=cv2.INTER_AREA)

def __generateBoundingBox(imap, reg, scale, t):
    """Use heatmap to generate bounding boxes"""
    stride = 2
    cellsize = 12

    imap = np.transpose(imap)
    dx1 = np.transpose(reg[:, :, 0])
    dy1 = np.transpose(reg[:, :, 1])
    dx2 = np.transpose(reg[:, :, 2])
    dy2 = np.transpose(reg[:, :, 3])
    y, x = np.where(imap >= t)
    if y.shape[0] == 1:
        dx1 = np.flipud(dx1)
        dy1 = np.flipud(dy1)
        dx2 = np.flipud(dx2)
        dy2 = np.flipud(dy2)
    score = imap[(y, x)]
    reg = np.transpose(np.vstack([dx1[(y, x)], dy1[(y, x)], dx2[(y, x)], dy2[(y, x)]]))
    if reg.size == 0:
        reg = np.empty((0, 3))
    bb = np.transpose(np.vstack([y, x]))
    q1 = np.fix((stride * bb + 1) / scale)
    q2 = np.fix((stride * bb + cellsize - 1 + 1) / scale)
    boundingbox = np.hstack([q1, q2, np.expand_dims(score, 1), reg])
    return boundingbox, reg


def __nms(boxes, threshold, method):
    if boxes.size == 0:
        return np.empty((0, 3))
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    s = boxes[:, 4]
    area = (x2 - x1 + 1) * (y2 - y1 + 1)
    I = np.argsort(s)
    pick = np.zeros_like(s, dtype=np.int16)
    counter = 0
    while I.size > 0:
        i = I[-1]
        pick[counter] = i
        counter += 1
        idx = I[0:-1]
        xx1 = np.maximum(x1[i], x1[idx])
        yy1 = np.maximum(y1[i], y1[idx])
        xx2 = np.minimum(x2[i], x2[idx])
        yy2 = np.minimum(y2[i], y2[idx])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        if method is 'Min':
            o = inter / np.minimum(area[i], area[idx])
        else:
            o = inter / (area[i] + area[idx] - inter)
        I = I[np.where(o <= threshold)]
    pick = pick[0:counter]
    return pick


def __rerec(bboxA):
    """Convert bboxA to square."""
    h = bboxA[:, 3] - bboxA[:, 1]
    w = bboxA[:, 2] - bboxA[:, 0]
    l = np.maximum(w, h)
    bboxA[:, 0] = bboxA[:, 0] + w * 0.5 - l * 0.5
    bboxA[:, 1] = bboxA[:, 1] + h * 0.5 - l * 0.5
    bboxA[:, 2:4] = bboxA[:, 0:2] + np.transpose(np.tile(l, (2, 1)))
    return bboxA


def __pad(total_boxes, w, h):
    """Compute the padding coordinates (pad the bounding boxes to square)"""
    tmpw = (total_boxes[:, 2] - total_boxes[:, 0] + 1).astype(np.int32)
    tmph = (total_boxes[:, 3] - total_boxes[:, 1] + 1).astype(np.int32)
    numbox = total_boxes.shape[0]

    dx = np.ones((numbox), dtype=np.int32)
    dy = np.ones((numbox), dtype=np.int32)
    edx = tmpw.copy().astype(np.int32)
    edy = tmph.copy().astype(np.int32)

    x = total_boxes[:, 0].copy().astype(np.int32)
    y = total_boxes[:, 1].copy().astype(np.int32)
    ex = total_boxes[:, 2].copy().astype(np.int32)
    ey = total_boxes[:, 3].copy().astype(np.int32)

    tmp = np.where(ex > w)
    edx.flat[tmp] = np.expand_dims(-ex[tmp] + w + tmpw[tmp], 1)
    ex[tmp] = w

    tmp = np.where(ey > h)
    edy.flat[tmp] = np.expand_dims(-ey[tmp] + h + tmph[tmp], 1)
    ey[tmp] = h

    tmp = np.where(x < 1)
    dx.flat[tmp] = np.expand_dims(2 - x[tmp], 1)
    x[tmp] = 1

    tmp = np.where(y < 1)
    dy.flat[tmp] = np.expand_dims(2 - y[tmp], 1)
    y[tmp] = 1

    return dy, edy, dx, edx, y, ey, x, ex, tmpw, tmph


def __bbreg(boundingbox, reg):
    """Calibrate bounding boxes"""
    if reg.shape[1] == 1:
        reg = np.reshape(reg, (reg.shape[2], reg.shape[3]))

    w = boundingbox[:, 2] - boundingbox[:, 0] + 1
    h = boundingbox[:, 3] - boundingbox[:, 1] + 1
    b1 = boundingbox[:, 0] + reg[:, 0] * w
    b2 = boundingbox[:, 1] + reg[:, 1] * h
    b3 = boundingbox[:, 2] + reg[:, 2] * w
    b4 = boundingbox[:, 3] + reg[:, 3] * h
    boundingbox[:, 0:4] = np.transpose(np.vstack([b1, b2, b3, b4]))
    return boundingbox

def generate_input_string(image):
    image_data_arr = []
    for i in range(image.shape[0]):
        byte_io = BytesIO()
        img = Image.fromarray(image[i, :, :, :].astype(np.uint8).squeeze())

        img.save(byte_io, 'JPEG')
        byte_io.seek(0)
        image_data = byte_io.read()
        image_data_arr.append([image_data])
    return image_data_arr

def pnet_serving(image):
    host = '127.0.0.1'
    port = 9000

    channel = implementations.insecure_channel(host, port)
    stub = prediction_service_pb2.beta_create_PredictionService_stub(channel)

    request = predict_pb2.PredictRequest()

    request.model_spec.name = 'mtcnn'

    request.model_spec.signature_name = 'pnet_predict'

    image_data_arr = generate_input_string(image)
    image_data_arr = np.asarray(image_data_arr).squeeze(axis=1)
    num_image = image_data_arr.shape[0]

    dims = [tensor_shape_pb2.TensorShapeProto.Dim(size=num_image)]
    tensor_shape_proto = tensor_shape_pb2.TensorShapeProto(dim=dims)
    tensor_proto = tensor_pb2.TensorProto(
        dtype=types_pb2.DT_STRING,
        tensor_shape=tensor_shape_proto,
        string_val=[image_data for image_data in image_data_arr])
    request.inputs['images'].CopyFrom(tensor_proto)

    result = stub.Predict(request, 10.0)
    return [MakeNdarray(result.outputs['result1']),
            MakeNdarray(result.outputs['result2'])]

def rnet_serving(image):
    host = '127.0.0.1'
    port = 9000

    channel = implementations.insecure_channel(host, port)
    stub = prediction_service_pb2.beta_create_PredictionService_stub(channel)

    request = predict_pb2.PredictRequest()

    request.model_spec.name = 'mtcnn'

    request.model_spec.signature_name = 'rnet_predict'

    image_data_arr = generate_input_string(image)
    image_data_arr = np.asarray(image_data_arr).squeeze()
    num_image = image_data_arr.shape[0]

    dims = [tensor_shape_pb2.TensorShapeProto.Dim(size=num_image)]
    tensor_shape_proto = tensor_shape_pb2.TensorShapeProto(dim=dims)
    tensor_proto = tensor_pb2.TensorProto(
        dtype=types_pb2.DT_STRING,
        tensor_shape=tensor_shape_proto,
        string_val=[image_data for image_data in image_data_arr])
    request.inputs['images'].CopyFrom(tensor_proto)    

    result = stub.Predict(request, 10.0)
    return [MakeNdarray(result.outputs['result1']),
            MakeNdarray(result.outputs['result2'])]

def onet_serving(image):
    host = '127.0.0.1'
    port = 9000

    channel = implementations.insecure_channel(host, port)
    stub = prediction_service_pb2.beta_create_PredictionService_stub(channel)

    request = predict_pb2.PredictRequest()

    request.model_spec.name = 'mtcnn'

    request.model_spec.signature_name = 'onet_predict'

    image_data_arr = generate_input_string(image)
    image_data_arr = np.asarray(image_data_arr).squeeze()
    num_image = image_data_arr.shape[0]

    dims = [tensor_shape_pb2.TensorShapeProto.Dim(size=num_image)]
    tensor_shape_proto = tensor_shape_pb2.TensorShapeProto(dim=dims)
    tensor_proto = tensor_pb2.TensorProto(
        dtype=types_pb2.DT_STRING,
        tensor_shape=tensor_shape_proto,
        string_val=[image_data for image_data in image_data_arr])
    request.inputs['images'].CopyFrom(tensor_proto) 

    result = stub.Predict(request, 10.0)

    return [MakeNdarray(result.outputs['result1']),
            MakeNdarray(result.outputs['result2']),
            MakeNdarray(result.outputs['result3'])]

def detect_face(img, minsize=20, threshold=None, factor=0.709):
    if threshold is None:
        threshold = [0.6, 0.7, 0.7]

    factor_count = 0
    total_boxes = np.empty((0, 9))
    points = np.empty(0)
    h = img.shape[0]
    w = img.shape[1]
    minl = np.amin([h, w])
    m = 12.0 / minsize
    minl = minl * m
    # create scale pyramid
    scales = []
    while minl >= 12:
        scales += [m * np.power(factor, factor_count)]
        minl = minl * factor
        factor_count += 1

    # first stage
    for scale in scales:
        hs = int(np.ceil(h * scale))
        ws = int(np.ceil(w * scale))
        im_data = __imresample(img, (hs, ws))
        
        img_x = np.expand_dims(im_data, 0)
        img_y = np.transpose(img_x, (0, 2, 1, 3))
#         img_y = (img_y - 127.5) * 0.0078125

        out = pnet_serving(img_y)
        out0 = np.transpose(out[0], (0, 2, 1, 3))
        out1 = np.transpose(out[1], (0, 2, 1, 3))

        boxes, _ = __generateBoundingBox(out1[0, :, :, 1].copy(), out0[0, :, :, :].copy(), scale, threshold[0])

        # inter-scale nms
        pick = __nms(boxes.copy(), 0.5, 'Union')
        if boxes.size > 0 and pick.size > 0:
            boxes = boxes[pick, :]
            total_boxes = np.append(total_boxes, boxes, axis=0)

    numbox = total_boxes.shape[0]
    if numbox > 0:
        pick = __nms(total_boxes.copy(), 0.7, 'Union')
        total_boxes = total_boxes[pick, :]
        regw = total_boxes[:, 2] - total_boxes[:, 0]
        regh = total_boxes[:, 3] - total_boxes[:, 1]
        qq1 = total_boxes[:, 0] + total_boxes[:, 5] * regw
        qq2 = total_boxes[:, 1] + total_boxes[:, 6] * regh
        qq3 = total_boxes[:, 2] + total_boxes[:, 7] * regw
        qq4 = total_boxes[:, 3] + total_boxes[:, 8] * regh
        total_boxes = np.transpose(np.vstack([qq1, qq2, qq3, qq4, total_boxes[:, 4]]))
        total_boxes = __rerec(total_boxes.copy())
        total_boxes[:, 0:4] = np.fix(total_boxes[:, 0:4]).astype(np.int32)
        dy, edy, dx, edx, y, ey, x, ex, tmpw, tmph = __pad(total_boxes.copy(), w, h)

    numbox = total_boxes.shape[0]
    if numbox > 0:
        # second stage
        tempimg = np.zeros((24, 24, 3, numbox))
        for k in range(0, numbox):
            tmp = np.zeros((int(tmph[k]), int(tmpw[k]), 3))
            tmp[dy[k] - 1:edy[k], dx[k] - 1:edx[k], :] = img[y[k] - 1:ey[k], x[k] - 1:ex[k], :]
            if tmp.shape[0] > 0 and tmp.shape[1] > 0 or tmp.shape[0] == 0 and tmp.shape[1] == 0:
                tempimg[:, :, :, k] = __imresample(tmp, (24, 24))
            else:
                return np.empty()
        
        tempimg1 = np.transpose(tempimg, (3, 1, 0, 2))
#         tempimg = (tempimg - 127.5) * 0.0078125
        out = rnet_serving(tempimg1)
        out0 = np.transpose(out[0])
        out1 = np.transpose(out[1])
        score = out1[1, :]
        ipass = np.where(score > threshold[1])
        total_boxes = np.hstack([total_boxes[ipass[0], 0:4].copy(), np.expand_dims(score[ipass].copy(), 1)])
        mv = out0[:, ipass[0]]

        if total_boxes.shape[0] > 0:
            pick = __nms(total_boxes, 0.7, 'Union')
            total_boxes = total_boxes[pick, :]
            total_boxes = __bbreg(total_boxes.copy(), np.transpose(mv[:, pick]))
            total_boxes = __rerec(total_boxes.copy())

    numbox = total_boxes.shape[0]
    if numbox > 0:
        # third stage
        total_boxes = np.fix(total_boxes).astype(np.int32)
        dy, edy, dx, edx, y, ey, x, ex, tmpw, tmph = __pad(total_boxes.copy(), w, h)
        tempimg = np.zeros((48, 48, 3, numbox))
        for k in range(0, numbox):
            tmp = np.zeros((int(tmph[k]), int(tmpw[k]), 3))
            tmp[dy[k] - 1:edy[k], dx[k] - 1:edx[k], :] = img[y[k] - 1:ey[k], x[k] - 1:ex[k], :]
            if tmp.shape[0] > 0 and tmp.shape[1] > 0 or tmp.shape[0] == 0 and tmp.shape[1] == 0:
                tempimg[:, :, :, k] = __imresample(tmp, (48, 48))
            else:
                return np.empty()
        
        tempimg1 = np.transpose(tempimg, (3, 1, 0, 2))
#         tempimg = (tempimg - 127.5) * 0.0078125
        out = onet_serving(tempimg1)
        out0 = np.transpose(out[0])
        out1 = np.transpose(out[1])
        out2 = np.transpose(out[2])
        score = out2[1, :]
        points = out1
        ipass = np.where(score > threshold[2])
        points = points[:, ipass[0]]
        total_boxes = np.hstack([total_boxes[ipass[0], 0:4].copy(), np.expand_dims(score[ipass].copy(), 1)])
        mv = out0[:, ipass[0]]

        w = total_boxes[:, 2] - total_boxes[:, 0] + 1
        h = total_boxes[:, 3] - total_boxes[:, 1] + 1
        points[0:5, :] = np.tile(w, (5, 1)) * points[0:5, :] + np.tile(total_boxes[:, 0], (5, 1)) - 1
        points[5:10, :] = np.tile(h, (5, 1)) * points[5:10, :] + np.tile(total_boxes[:, 1], (5, 1)) - 1

        if total_boxes.shape[0] > 0:
            total_boxes = __bbreg(total_boxes.copy(), np.transpose(mv))
            pick = __nms(total_boxes.copy(), 0.7, 'Min')
            total_boxes = total_boxes[pick, :]
            points = points[:, pick]

    return total_boxes, points

def main(args):
    image_dir = args.image_dir
    files = os.listdir(image_dir)
    
    face_crop_margin = 10
    face_size = 160
        
    for file in files:
        file_name, file_ext = os.path.splitext(file)[0], os.path.splitext(file)[1]
        img = misc.imread(os.path.join(image_dir, file))
        bboxes, points = detect_face(img)

        for index, bbox in enumerate(bboxes):
            bbox_accuracy = bbox[4] * 100.0
            if bbox_accuracy < 99:
                continue

            w, h = img.shape[0:2]
            left = int(np.maximum(bbox[0] - face_crop_margin / 2, 0))
            top = int(np.maximum(bbox[1] - face_crop_margin / 2, 0))
            right = int(np.minimum(bbox[2] + face_crop_margin / 2, h))
            bottom = int(np.minimum(bbox[3] + face_crop_margin / 2, w))

            cropped = img[top:bottom, left:right, :]
            cropped_img = cv2.resize(cropped, (face_size, face_size), interpolation=cv2.INTER_LINEAR)
            
            save_file_name = '{}_crop_{}{}'.format(file_name, index, file_ext)
            misc.imsave(os.path.join(image_dir, save_file_name), cropped_img)

def parse_argument(argv):
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--image_dir', type=str, help='Directory with unaligned images.')
    
    return parser.parse_args(argv)
        
if __name__ == '__main__':
    main(parse_argument(sys.argv[1:]))

# python ./mtcnn_client.py --image_dir='/home/lzhang/tmp/0000045'
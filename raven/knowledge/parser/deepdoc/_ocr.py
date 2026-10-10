"""Finding the text on a rendered page, and reading it.

Two graphs. The detector answers with quadrilaterals around every run of text
it can see; the recogniser reads one crop at a time and answers with
characters. PaddleOCR's pair, as RAGFlow publishes them.

Ported from RAGFlow's ``deepdoc/vision/ocr.py`` (Apache-2.0; see NOTICES.md).
Three things are left behind. The recogniser class carries nine resize
routines, one per architecture PaddleOCR can export; the published weights are
the CRNN-shaped one, so the other eight would be code no call could reach. The
multi-device pool is gone for the reason given in :mod:`._onnx`. And the retry
loop that swallows an inference failure a hundred thousand times, sleeping five
seconds between attempts, is now three attempts -- a page that cannot be read
should not hold the queue for six days.
"""

from __future__ import annotations

import copy
import math
import time

import cv2
import numpy as np
from loguru import logger

from raven.knowledge.parser.deepdoc._onnx import RES_DIR, session
from raven.knowledge.parser.deepdoc._operators import (
    DetResizeForTest,
    KeepKeys,
    NormalizeImage,
    ToCHWImage,
)
from raven.knowledge.parser.deepdoc._postprocess import CTCLabelDecode, DBPostProcess

#: How many times an inference is retried before the page is given up on, and
#: how long it waits between. Upstream retries a hundred thousand times with a
#: five-second sleep -- six days for one page -- on the theory that the failure
#: is a busy GPU. Three attempts: a transient allocation failure clears in
#: seconds, and anything that does not is not going to.
_ATTEMPTS = 3
_BACKOFF_S = 1.0


def _run_with_retry(predictor, inputs):
    """One inference, retried briefly, then raised."""
    for attempt in range(_ATTEMPTS):
        try:
            return predictor.run(None, inputs, None)
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced
            if attempt == _ATTEMPTS - 1:
                raise
            logger.debug("deepdoc: inference failed ({}); retrying", exc)
            time.sleep(_BACKOFF_S)
    raise RuntimeError("unreachable")


def transform(data, ops=None):
    """Apply the preparation operators to one image, in order."""
    for op in ops or []:
        data = op(data)
        if data is None:
            return None
    return data


class TextRecognizer:
    def __init__(self) -> None:
        self.rec_image_shape = [int(v) for v in "3, 48, 320".split(",")]
        self.rec_batch_num = 16
        self.postprocess_op = CTCLabelDecode(
            character_dict_path=str(RES_DIR / "ocr.res"),
            use_space_char=True,
        )
        self.predictor = session("rec")
        self.input_tensor = self.predictor.get_inputs()[0]

    def resize_norm_img(self, img, max_wh_ratio):
        imgC, imgH, imgW = self.rec_image_shape

        assert imgC == img.shape[2]
        imgW = int((imgH * max_wh_ratio))
        w = self.input_tensor.shape[3:][0]
        if isinstance(w, str):
            pass
        elif w is not None and w > 0:
            imgW = w
        h, w = img.shape[:2]
        ratio = w / float(h)
        if math.ceil(imgH * ratio) > imgW:
            resized_w = imgW
        else:
            resized_w = int(math.ceil(imgH * ratio))

        resized_image = cv2.resize(img, (resized_w, imgH))
        resized_image = resized_image.astype("float32")
        resized_image = resized_image.transpose((2, 0, 1)) / 255
        resized_image -= 0.5
        resized_image /= 0.5
        padding_im = np.zeros((imgC, imgH, imgW), dtype=np.float32)
        padding_im[:, :, 0:resized_w] = resized_image
        return padding_im

    def __call__(self, img_list):
        img_num = len(img_list)
        # Calculate the aspect ratio of all text bars
        width_list = []
        for img in img_list:
            width_list.append(img.shape[1] / float(img.shape[0]))
        # Sorting can speed up the recognition process
        indices = np.argsort(np.array(width_list))
        rec_res = [["", 0.0]] * img_num
        batch_num = self.rec_batch_num
        st = time.time()

        for beg_img_no in range(0, img_num, batch_num):
            end_img_no = min(img_num, beg_img_no + batch_num)
            norm_img_batch = []
            imgC, imgH, imgW = self.rec_image_shape[:3]
            max_wh_ratio = imgW / imgH
            # max_wh_ratio = 0
            for ino in range(beg_img_no, end_img_no):
                h, w = img_list[indices[ino]].shape[0:2]
                wh_ratio = w * 1.0 / h
                max_wh_ratio = max(max_wh_ratio, wh_ratio)
            for ino in range(beg_img_no, end_img_no):
                norm_img = self.resize_norm_img(img_list[indices[ino]], max_wh_ratio)
                norm_img = norm_img[np.newaxis, :]
                norm_img_batch.append(norm_img)
            norm_img_batch = np.concatenate(norm_img_batch)
            norm_img_batch = norm_img_batch.copy()

            input_dict = {}
            input_dict[self.input_tensor.name] = norm_img_batch
            outputs = _run_with_retry(self.predictor, input_dict)
            preds = outputs[0]
            rec_result = self.postprocess_op(preds)
            for rno in range(len(rec_result)):
                rec_res[indices[beg_img_no + rno]] = rec_result[rno]

        return rec_res, time.time() - st


class TextDetector:
    def __init__(self, *, limit_side_len: int = 2048) -> None:
        """Initialize the text detector.

        Args:
            model_dir: Path to the ONNX model directory.
            device_id: GPU device id; None / 0 = CPU. Kept as the 2nd positional
                arg for backwards compatibility with the four internal call sites
                in :class:`OCR` and any external callers.
            limit_side_len: Upper bound on the longer image side after the
                ``DetResizeForTest`` downscale. Only used for dynamic-input models
                (when ``self.input_tensor.shape[2:]`` is symbolic); for
                fixed-shape models the pre-process list is replaced with
                ``image_shape`` and ``limit_side_len`` is ignored — see the
                comment near the ``pre_process_list[0] = ...`` assignment below.

        ``limit_side_len`` is keyword-only so a future caller writing
        ``TextDetector(model_dir, 2048)`` cannot silently bind ``2048`` to
        ``device_id``. (See PR #18888 review comment from xugangqiang.)
        """
        pre_process_list = [
            {
                "DetResizeForTest": {
                    "limit_side_len": limit_side_len,
                    "limit_type": "max",
                }
            },
            {
                "NormalizeImage": {
                    "std": [0.229, 0.224, 0.225],
                    "mean": [0.485, 0.456, 0.406],
                    "scale": "1./255.",
                    "order": "hwc",
                }
            },
            {"ToCHWImage": None},
            {"KeepKeys": {"keep_keys": ["image", "shape"]}},
        ]
        postprocess_params = {
            "name": "DBPostProcess",
            "thresh": 0.3,
            "box_thresh": 0.5,
            "max_candidates": 1000,
            "unclip_ratio": 1.5,
            "use_dilation": False,
            "score_mode": "fast",
            "box_type": "quad",
        }

        self.postprocess_op = DBPostProcess(**{k: v for k, v in postprocess_params.items() if k != "name"})
        self.predictor = session("det")
        self.input_tensor = self.predictor.get_inputs()[0]

        img_h, img_w = self.input_tensor.shape[2:]
        if isinstance(img_h, str) or isinstance(img_w, str):
            # Dynamic-shape model: ``limit_side_len`` is the resize cap; keep
            # the first pre-process entry as built above.
            pass
        elif img_h is not None and img_w is not None and img_h > 0 and img_w > 0:
            # Fixed-shape ONNX model: the model forces a concrete input size,
            # so ``limit_side_len`` is ignored and we resize to the model's
            # own shape. This is intentional — overriding ``image_shape``
            # with ``limit_side_len`` would crash the model. See PR #18888
            # review (xugangqiang) — fixed-shape models silently ignore
            # ``limit_side_len`` and the contract is now documented here.
            pre_process_list[0] = {"DetResizeForTest": {"image_shape": [img_h, img_w]}}
        self.preprocess_op = [
            DetResizeForTest(**pre_process_list[0]["DetResizeForTest"]),
            NormalizeImage(**pre_process_list[1]["NormalizeImage"]),
            ToCHWImage(),
            KeepKeys(**pre_process_list[3]["KeepKeys"]),
        ]

    def order_points_clockwise(self, pts):
        rect = np.zeros((4, 2), dtype="float32")
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)]
        rect[2] = pts[np.argmax(s)]
        tmp = np.delete(pts, (np.argmin(s), np.argmax(s)), axis=0)
        diff = np.diff(np.array(tmp), axis=1)
        rect[1] = tmp[np.argmin(diff)]
        rect[3] = tmp[np.argmax(diff)]
        return rect

    def clip_det_res(self, points, img_height, img_width):
        for pno in range(points.shape[0]):
            points[pno, 0] = int(min(max(points[pno, 0], 0), img_width - 1))
            points[pno, 1] = int(min(max(points[pno, 1], 0), img_height - 1))
        return points

    def filter_tag_det_res(self, dt_boxes, image_shape):
        img_height, img_width = image_shape[0:2]
        dt_boxes_new = []
        for box in dt_boxes:
            if isinstance(box, list):
                box = np.array(box)
            box = self.order_points_clockwise(box)
            box = self.clip_det_res(box, img_height, img_width)
            rect_width = int(np.linalg.norm(box[0] - box[1]))
            rect_height = int(np.linalg.norm(box[0] - box[3]))
            if rect_width <= 3 or rect_height <= 3:
                continue
            dt_boxes_new.append(box)
        dt_boxes = np.array(dt_boxes_new)
        return dt_boxes

    def filter_tag_det_res_only_clip(self, dt_boxes, image_shape):
        img_height, img_width = image_shape[0:2]
        dt_boxes_new = []
        for box in dt_boxes:
            if isinstance(box, list):
                box = np.array(box)
            box = self.clip_det_res(box, img_height, img_width)
            dt_boxes_new.append(box)
        dt_boxes = np.array(dt_boxes_new)
        return dt_boxes

    def __call__(self, img):
        ori_im = img.copy()
        data = {"image": img}

        st = time.time()
        data = transform(data, self.preprocess_op)
        img, shape_list = data
        if img is None:
            return None, 0
        img = np.expand_dims(img, axis=0)
        shape_list = np.expand_dims(shape_list, axis=0)
        img = img.copy()
        input_dict = {}
        input_dict[self.input_tensor.name] = img
        outputs = _run_with_retry(self.predictor, input_dict)
        post_result = self.postprocess_op({"maps": outputs[0]}, shape_list)
        dt_boxes = post_result[0]["points"]
        dt_boxes = self.filter_tag_det_res(dt_boxes, ori_im.shape)

        return dt_boxes, time.time() - st


class OCR:
    def __init__(self) -> None:
        """The detector and the recogniser, sharing this process's sessions."""
        self.text_detector = [TextDetector()]
        self.text_recognizer = [TextRecognizer()]
        self.drop_score = 0.5
        self.crop_image_res_index = 0

    def get_rotate_crop_image(self, img, points):
        """One detected quadrilateral, warped flat into an upright strip.

        The detector answers with four corners rather than a rectangle, because
        a line of text on a page that was scanned at an angle is not one. The
        recognizer reads left to right, so the quad is projected onto a
        rectangle of its own measured size before it is read.
        """
        assert len(points) == 4, "shape of points must be 4*2"
        points = np.asarray(points, dtype=np.float32)
        img_crop_width = int(max(np.linalg.norm(points[0] - points[1]), np.linalg.norm(points[2] - points[3])))
        img_crop_height = int(max(np.linalg.norm(points[0] - points[3]), np.linalg.norm(points[1] - points[2])))
        pts_std = np.array(
            [[0, 0], [img_crop_width, 0], [img_crop_width, img_crop_height], [0, img_crop_height]],
            dtype=np.float32,
        )
        M = cv2.getPerspectiveTransform(points, pts_std)
        dst_img = cv2.warpPerspective(
            img, M, (img_crop_width, img_crop_height), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC
        )
        dst_img_height, dst_img_width = dst_img.shape[0:2]
        if dst_img_height * 1.0 / dst_img_width >= 1.5:
            # Try original orientation
            rec_result = self.text_recognizer[0]([dst_img])
            text, score = rec_result[0][0]
            best_score = score
            best_img = dst_img

            # Try clockwise 90° rotation
            rotated_cw = np.rot90(dst_img, k=3)
            rec_result = self.text_recognizer[0]([rotated_cw])
            rotated_cw_text, rotated_cw_score = rec_result[0][0]
            if rotated_cw_score > best_score:
                best_score = rotated_cw_score
                best_img = rotated_cw

            # Try counter-clockwise 90° rotation
            rotated_ccw = np.rot90(dst_img, k=1)
            rec_result = self.text_recognizer[0]([rotated_ccw])
            rotated_ccw_text, rotated_ccw_score = rec_result[0][0]
            if rotated_ccw_score > best_score:
                best_img = rotated_ccw

            # Use the best image
            dst_img = best_img
        return dst_img

    def sorted_boxes(self, dt_boxes):
        """
        Sort text boxes in order from top to bottom, left to right
        args:
            dt_boxes(array):detected text boxes with shape [4, 2]
        return:
            sorted boxes(array) with shape [4, 2]
        """
        num_boxes = dt_boxes.shape[0]
        sorted_boxes = sorted(dt_boxes, key=lambda x: (x[0][1], x[0][0]))
        _boxes = list(sorted_boxes)

        for i in range(num_boxes - 1):
            for j in range(i, -1, -1):
                if abs(_boxes[j + 1][0][1] - _boxes[j][0][1]) < 10 and (_boxes[j + 1][0][0] < _boxes[j][0][0]):
                    tmp = _boxes[j]
                    _boxes[j] = _boxes[j + 1]
                    _boxes[j + 1] = tmp
                else:
                    break
        return _boxes

    def detect(self, img, device_id: int | None = None):
        if device_id is None:
            device_id = 0

        if img is None:
            return None

        dt_boxes, _ = self.text_detector[device_id](img)

        if dt_boxes is None:
            return None

        return zip(self.sorted_boxes(dt_boxes), [("", 0) for _ in range(len(dt_boxes))])

    def recognize(self, ori_im, box, device_id: int | None = None):
        if device_id is None:
            device_id = 0

        img_crop = self.get_rotate_crop_image(ori_im, box)

        rec_res, elapse = self.text_recognizer[device_id]([img_crop])
        text, score = rec_res[0]
        if score < self.drop_score:
            return ""
        return text

    def recognize_batch(self, img_list, device_id: int | None = None):
        if device_id is None:
            device_id = 0
        rec_res, elapse = self.text_recognizer[device_id](img_list)
        texts = []
        for i in range(len(rec_res)):
            text, score = rec_res[i]
            if score < self.drop_score:
                text = ""
            texts.append(text)
        return texts

    def recognize_batch_with_score(self, img_list, device_id: int | None = None):
        """Like recognize_batch but keeps the per-item recognition score.

        Returns a list of (text, score) tuples. Text below drop_score is
        blanked, matching recognize_batch, but the score is preserved so the
        caller (the OCR HTTP adapter) can surface it for score-based layer-2
        rotation selection. Adding this method instead of changing
        recognize_batch keeps the in-process __ocr path (pdf_parser.py) on the
        original text-only contract.
        """
        if device_id is None:
            device_id = 0
        rec_res, elapse = self.text_recognizer[device_id](img_list)
        out = []
        for i in range(len(rec_res)):
            text, score = rec_res[i]
            if score < self.drop_score:
                text = ""
            out.append((text, score))
        return out

    def __call__(self, img, device_id=0, cls=True):
        time_dict = {"det": 0, "rec": 0, "cls": 0, "all": 0}
        if device_id is None:
            device_id = 0

        if img is None:
            return None, None, time_dict

        start = time.time()
        ori_im = img.copy()
        dt_boxes, elapse = self.text_detector[device_id](img)
        time_dict["det"] = elapse

        if dt_boxes is None:
            end = time.time()
            time_dict["all"] = end - start
            return None, None, time_dict

        img_crop_list = []

        dt_boxes = self.sorted_boxes(dt_boxes)

        for bno in range(len(dt_boxes)):
            tmp_box = copy.deepcopy(dt_boxes[bno])
            img_crop = self.get_rotate_crop_image(ori_im, tmp_box)
            img_crop_list.append(img_crop)

        rec_res, elapse = self.text_recognizer[device_id](img_crop_list)

        time_dict["rec"] = elapse

        filter_boxes, filter_rec_res = [], []
        for box, rec_result in zip(dt_boxes, rec_res):
            text, score = rec_result
            if score >= self.drop_score:
                filter_boxes.append(box)
                filter_rec_res.append(rec_result)
        end = time.time()
        time_dict["all"] = end - start

        # for bno in range(len(img_crop_list)):
        #    print(f"{bno}, {rec_res[bno]}")

        return list(zip([a.tolist() for a in filter_boxes], filter_rec_res))

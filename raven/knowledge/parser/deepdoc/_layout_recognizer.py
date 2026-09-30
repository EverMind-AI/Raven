"""What each region of a page is: a heading, a table, a footer, a figure.

The model this runs answers with boxes and one of eleven labels, and that is
the part of deepdoc a geometric parser cannot reproduce. Reading order, column
structure and "is this line a heading or just large" are all inferences from
appearance, and the layout model was trained on pages where they were marked.

Ported from RAGFlow's ``deepdoc/vision/layout_recognizer.py`` (Apache-2.0; see
NOTICES.md). Without the remote-inference client and the Ascend NPU variant it
carries beside this class: both are deployment shapes this tree does not have,
and an unused branch behind an environment variable is one nothing here would
ever exercise.
"""

from __future__ import annotations

import copy
import re
from collections import Counter

import cv2
import numpy as np
from loguru import logger

from raven.knowledge.parser.deepdoc._operators import nms
from raven.knowledge.parser.deepdoc._recognizer import Recognizer


class LayoutRecognizer(Recognizer):
    #: The classes the published model answers with, in its own order.
    #:
    #: Not the list RAGFlow carries beside this class. That one starts with a
    #: background class and runs Text, Title, Figure; the weights on the hub
    #: today are a DocLayout-YOLO export whose order is Title, Text, Abandon,
    #: and the two disagree on every index. Read against its own code, a title
    #: comes back as `_background_` and a page footer as a title.
    #:
    #: Established by running the model rather than by reading either list:
    #: a page carrying a header, a title, prose, a chart, a table and a page
    #: number answers 2, 0, 1, 3, 5, 6 in that order, which is DocLayout-YOLO's
    #: published set and nothing else.
    labels = [
        "Title",
        "Text",
        # Headers, footers, page numbers -- the furniture a page repeats and a
        # reader does not read. One class for all of it, which is why the
        # garbage filter below names it rather than the three separate labels
        # the older model had.
        "Abandon",
        "Figure",
        "Figure caption",
        "Table",
        "Table caption",
        "Table footnote",
        "Equation",
        "Equation caption",
    ]

    def __init__(self, domain: str = "layout"):
        """
        Args:
            domain (`str`):
                Which layout model to run. RAGFlow swaps in a genre-specific
                one (`layout.laws`, `layout.manual`, `layout.paper`) per parser
                it ships; nothing here selects between them, so the general
                model is the default and the others are not fetched.
        """
        # What the older model split into footer, header and reference, this
        # one answers as one class.
        self.garbage_layouts = ["abandon"]
        self.client = None
        super().__init__(self.labels, domain)

    def __call__(self, image_list, ocr_res, scale_factor=3, thr=0.2, batch_size=16, drop=True):
        def __is_garbage(b):
            patt = [r"\(cid\s*:\s*\d+\s*\)"]
            return any([re.search(p, b.get("text", "")) for p in patt])

        layouts = super().__call__(image_list, thr, batch_size)
        assert len(image_list) == len(ocr_res)
        # Tag layout type
        boxes = []
        assert len(image_list) == len(layouts)
        garbages = {}
        page_layout = []
        for pn, lts in enumerate(layouts):
            bxs = ocr_res[pn]
            lts = [
                {
                    "type": b["type"],
                    "score": float(b["score"]),
                    "x0": b["bbox"][0] / scale_factor,
                    "x1": b["bbox"][2] / scale_factor,
                    "top": b["bbox"][1] / scale_factor,
                    "bottom": b["bbox"][-1] / scale_factor,
                    "page_number": pn,
                }
                for b in self._filter_garbage_layouts(lts)
            ]
            lts = self.sort_Y_firstly(lts, np.mean([lt["bottom"] - lt["top"] for lt in lts]) / 2)
            lts = self.layouts_cleanup(bxs, lts)
            page_layout.append(lts)

            def findLayout(ty):
                nonlocal bxs, lts, self
                lts_ = [lt for lt in lts if lt["type"] == ty]
                i = 0
                while i < len(bxs):
                    if bxs[i].get("layout_type"):
                        i += 1
                        continue
                    if __is_garbage(bxs[i]):
                        bxs.pop(i)
                        continue

                    ii = self.find_overlapped_with_threshold(bxs[i], lts_, thr=0.4)
                    if ii is None:
                        bxs[i]["layout_type"] = ""
                        i += 1
                        continue
                    lts_[ii]["visited"] = True
                    keep_feats = [
                        lts_[ii]["type"] == "footer" and bxs[i]["bottom"] < image_list[pn].size[1] * 0.9 / scale_factor,
                        lts_[ii]["type"] == "header" and bxs[i]["top"] > image_list[pn].size[1] * 0.1 / scale_factor,
                    ]
                    if drop and lts_[ii]["type"] in self.garbage_layouts and not any(keep_feats):
                        if lts_[ii]["type"] not in garbages:
                            garbages[lts_[ii]["type"]] = []
                        garbages[lts_[ii]["type"]].append(bxs[i]["text"])
                        bxs.pop(i)
                        continue

                    bxs[i]["layoutno"] = f"{ty}-{ii}"
                    bxs[i]["layout_type"] = lts_[ii]["type"] if lts_[ii]["type"] != "equation" else "figure"
                    i += 1

            for lt in [
                "footer",
                "header",
                "reference",
                "figure caption",
                "table caption",
                "title",
                "table",
                "text",
                "figure",
                "equation",
            ]:
                findLayout(lt)

            # add box to figure/equation layouts which have no text box.
            # Index within each type's own list and keep the type as the layoutno
            # prefix so these match the namespace findLayout() assigns to
            # text-overlapping boxes (figure-N vs equation-N). Using a combined
            # figure+equation index with a fixed "figure" prefix would collide
            # with figure-N tags from findLayout and merge unrelated regions.
            for ty in ["figure", "equation"]:
                for i, lt in enumerate([lt for lt in lts if lt["type"] == ty]):
                    if lt.get("visited"):
                        continue
                    lt = copy.deepcopy(lt)
                    lt.pop("type", None)
                    lt["text"] = ""
                    lt["layout_type"] = "figure"
                    lt["layoutno"] = f"{ty}-{i}"
                    logger.debug("deepdoc: placeholder box {} for a textless {} region", lt["layoutno"], ty)
                    bxs.append(lt)

            boxes.extend(bxs)

        ocr_res = boxes

        garbag_set = set()
        for k in garbages.keys():
            garbages[k] = Counter(garbages[k])
            for g, c in garbages[k].items():
                if c > 1:
                    garbag_set.add(g)

        ocr_res = [b for b in ocr_res if b["text"].strip() not in garbag_set]
        return ocr_res, page_layout

    def _filter_garbage_layouts(self, boxes, score_thr=0.4):
        """Drop garbage-layout boxes (footer/header/reference) below ``score_thr``.

        Mirrors the gate applied in ``__call__`` (layout_recognizer.py:97 and
        :379) so that every consumer of ``forward`` — notably the
        ``/predict/dla`` HTTP endpoint served by ``DLAAdapter`` — receives
        Python-production-aligned output instead of raw low-confidence garbage
        boxes. Non-garbage boxes are always kept regardless of score.
        """
        return [b for b in boxes if float(b["score"]) >= score_thr or b["type"] not in self.garbage_layouts]

    def forward(self, image_list, thr=0.7, batch_size=16):
        layouts = super().__call__(image_list, thr, batch_size)
        # Apply the 0.4 garbage gate so forward output matches __call__; the
        # /predict/dla endpoint otherwise returns unfiltered low-confidence
        # garbage (see _filter_garbage_layouts).
        return [self._filter_garbage_layouts(page_boxes) for page_boxes in layouts]


class LayoutRecognizer4YOLOv10(LayoutRecognizer):
    labels = [
        "title",
        "Text",
        "Reference",
        "Figure",
        "Figure caption",
        "Table",
        "Table caption",
        "Table caption",
        "Equation",
        "Figure caption",
    ]

    def __init__(self, domain):
        domain = "layout"
        super().__init__(domain)
        self.auto = False
        self.scaleFill = False
        self.scaleup = True
        self.stride = 32
        self.center = True

    def preprocess(self, image_list):
        inputs = []
        new_shape = self.input_shape  # height, width
        for img in image_list:
            shape = img.shape[:2]  # current shape [height, width]
            # Scale ratio (new / old)
            r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
            # Compute padding
            new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
            dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]  # wh padding
            dw /= 2  # divide padding into 2 sides
            dh /= 2
            ww, hh = new_unpad
            img = np.array(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).astype(np.float32)
            img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
            top, bottom = int(round(dh - 0.1)) if self.center else 0, int(round(dh + 0.1))
            left, right = int(round(dw - 0.1)) if self.center else 0, int(round(dw + 0.1))
            img = cv2.copyMakeBorder(
                img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114)
            )  # add border
            img /= 255.0
            img = img.transpose(2, 0, 1)
            img = img[np.newaxis, :, :, :].astype(np.float32)
            inputs.append({self.input_names[0]: img, "scale_factor": [shape[1] / ww, shape[0] / hh, dw, dh]})

        return inputs

    def postprocess(self, boxes, inputs, thr):
        thr = 0.08
        boxes = np.squeeze(boxes)
        scores = boxes[:, 4]
        boxes = boxes[scores > thr, :]
        scores = scores[scores > thr]
        if len(boxes) == 0:
            return []
        class_ids = boxes[:, -1].astype(int)
        boxes = boxes[:, :4]
        boxes[:, 0] -= inputs["scale_factor"][2]
        boxes[:, 2] -= inputs["scale_factor"][2]
        boxes[:, 1] -= inputs["scale_factor"][3]
        boxes[:, 3] -= inputs["scale_factor"][3]
        input_shape = np.array(
            [inputs["scale_factor"][0], inputs["scale_factor"][1], inputs["scale_factor"][0], inputs["scale_factor"][1]]
        )
        boxes = np.multiply(boxes, input_shape, dtype=np.float32)

        unique_class_ids = np.unique(class_ids)
        indices = []
        for class_id in unique_class_ids:
            class_indices = np.where(class_ids == class_id)[0]
            class_boxes = boxes[class_indices, :]
            class_scores = scores[class_indices]
            class_keep_boxes = nms(class_boxes, class_scores, 0.45)
            indices.extend(class_indices[class_keep_boxes])

        return [
            {
                "type": self.label_list[class_ids[i]].lower(),
                "bbox": [float(t) for t in boxes[i].tolist()],
                "score": float(scores[i]),
            }
            for i in indices
        ]

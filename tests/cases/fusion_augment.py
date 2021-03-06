from .provider_test import TestWithTempFiles
from neurolight.gunpowder.swc_file_source import SwcFileSource, SwcPoint
from neurolight.gunpowder.fusion_augment import FusionAugment
from neurolight.gunpowder.rasterize_skeleton import RasterizeSkeleton
from gunpowder import (
    PointsKey,
    PointsSpec,
    ArrayKey,
    ArraySpec,
    BatchRequest,
    Roi,
    build,
    Coordinate,
    MergeProvider,
)

import numpy as np

try:
    from spimagine import volshow

    imported_volshow = True
except Exception:
    imported_volshow = False

from typing import Dict, List, Tuple, Optional
from pathlib import Path


class FusionAugmentTest(TestWithTempFiles):
    def setUp(self):
        super(FusionAugmentTest, self).setUp()

    def _write_swc(
        self,
        file_path: Path,
        points: List[SwcPoint],
        constants: Dict[str, Coordinate] = {},
    ):
        swc = ""
        for key, shape in constants.items():
            swc += "# {} {}\n".format(key.upper(), " ".join([str(x) for x in shape]))
        swc += "\n".join(
            [
                "{} {} {} {} {} {} {}".format(
                    p.point_id, p.point_type, *p.location, p.radius, p.parent_id
                )
                for p in points
            ]
        )
        with file_path.open("w") as f:
            f.write(swc)

    def _get_points(
        self, inside: np.ndarray, slope: np.ndarray, bb: Roi
    ) -> List[SwcPoint]:
        slope = slope / max(slope)
        shape = np.array(bb.get_shape())
        outside_down = inside - shape * slope
        outside_up = inside + shape * slope
        down_intercept = self._resample_relative(inside, outside_down, bb)
        up_intercept = self._resample_relative(inside, outside_up, bb)

        points = [
            # line
            SwcPoint(0, 0, down_intercept, 0, 0),
            SwcPoint(1, 0, up_intercept, 0, 0),
        ]
        return points

    def _resample_relative(
        self, inside: np.ndarray, outside: np.ndarray, bb: Roi
    ) -> Optional[np.ndarray]:
        offset = outside - inside
        # get_end() is not contained in the Roi. We want the point to be included,
        # thus we decriment by 1. Technically we only need to decriment by 0.000001,
        # but that is not possible using Roi's and Coordinates. Should we change this?
        bb_x = np.asarray(
            [
                (np.asarray(bb.get_begin()) - inside) / offset,
                (np.asarray(bb.get_end() - Coordinate([1, 1, 1])) - inside) / offset,
            ]
        )

        if np.sum(np.logical_and((bb_x > 0), (bb_x <= 1))) > 0:
            s = np.min(bb_x[np.logical_and((bb_x > 0), (bb_x <= 1))])
            return np.array(inside) + s * offset
        else:
            return None

    def _get_line_pair(
        self,
        roi: Roi = Roi(Coordinate([0, 0, 0]), Coordinate([10, 10, 10])),
        dist: float = 3,
    ) -> Tuple[Tuple[np.ndarray, np.ndarray], Tuple[np.ndarray, np.ndarray]]:
        bb_size = np.array(roi.get_shape()) - Coordinate([1, 1, 1])
        pad = min(dist / np.array(bb_size))
        center = np.random.random((3,)).clip(pad, 1 - pad) * (bb_size)
        slope = np.random.random((3,))
        slope /= np.linalg.norm(slope)

        intercepts = (center + slope * dist / 2, center - slope * dist / 2)
        slope_a = np.random.random(3)
        slope_a -= np.dot(slope_a, slope) * slope
        slope_a /= np.linalg.norm(slope_a)
        slope_b = np.cross(slope_a, slope)

        return (intercepts, (slope_a, slope_b))

    def test_get_line_pair(self):
        dist = 3
        intercepts, slopes = self._get_line_pair(
            roi=Roi(Coordinate([0, 0, 0]), Coordinate([10, 10, 10])), dist=dist
        )
        a, b = intercepts

        # check that the line connecting the closest points is perp to both slopes
        self.assertAlmostEqual(np.linalg.norm(np.dot(b - a, slopes[0])), 0)
        self.assertAlmostEqual(np.linalg.norm(np.dot(b - a, slopes[1])), 0)
        # check the intercepts are the expected distance
        self.assertAlmostEqual(np.linalg.norm(intercepts[1] - intercepts[0]) - dist, 0)

    def test_two_disjoin_lines_intensity(self):
        # This is worryingly slow for such a small volume (256**3) and only 2
        # straight lines for skeletons.
        LABEL_RADIUS = 3
        RAW_RADIUS = 3
        BLEND_SMOOTHNESS = 3

        bb = Roi(Coordinate([0, 0, 0]), ([256, 256, 256]))
        voxel_size = Coordinate([1, 1, 1])
        swc_files = ("test_line_a.swc", "test_line_b.swc")
        swc_paths = tuple(Path(self.path_to(file_name)) for file_name in swc_files)

        # create two lines seperated by a given distance and write them to swc files
        intercepts, slopes = self._get_line_pair(roi=bb, dist=3 * LABEL_RADIUS)
        for intercept, slope, swc_path in zip(intercepts, slopes, swc_paths):
            swc_points = self._get_points(intercept, slope, bb)
            self._write_swc(swc_path, swc_points)

        # create swc sources
        fused = ArrayKey("FUSED")
        fused_labels = ArrayKey("FUSED_LABELS")
        swc_key_names = ("SWC_A", "SWC_B")
        labels_key_names = ("LABELS_A", "LABELS_B")
        raw_key_names = ("RAW_A", "RAW_B")

        swc_keys = tuple(PointsKey(name) for name in swc_key_names)
        labels_keys = tuple(ArrayKey(name) for name in labels_key_names)
        raw_keys = tuple(ArrayKey(name) for name in raw_key_names)

        # add request
        request = BatchRequest()
        request.add(fused, bb.get_shape())
        request.add(fused_labels, bb.get_shape())
        request.add(labels_keys[0], bb.get_shape())
        request.add(labels_keys[1], bb.get_shape())
        request.add(raw_keys[0], bb.get_shape())
        request.add(raw_keys[1], bb.get_shape())
        request.add(swc_keys[0], bb.get_shape())
        request.add(swc_keys[1], bb.get_shape())

        # data source for swc a
        data_sources_a = tuple()
        data_sources_a = (
            data_sources_a
            + SwcFileSource(swc_paths[0], swc_keys[0], PointsSpec(roi=bb))
            + RasterizeSkeleton(
                points=swc_keys[0],
                array=labels_keys[0],
                array_spec=ArraySpec(
                    interpolatable=False, dtype=np.uint32, voxel_size=voxel_size
                ),
                radius=LABEL_RADIUS,
            )
            + RasterizeSkeleton(
                points=swc_keys[0],
                array=raw_keys[0],
                array_spec=ArraySpec(
                    interpolatable=False, dtype=np.uint32, voxel_size=voxel_size
                ),
                radius=RAW_RADIUS,
            )
        )

        # data source for swc b
        data_sources_b = tuple()
        data_sources_b = (
            data_sources_b
            + SwcFileSource(swc_paths[1], swc_keys[1], PointsSpec(roi=bb))
            + RasterizeSkeleton(
                points=swc_keys[1],
                array=labels_keys[1],
                array_spec=ArraySpec(
                    interpolatable=False, dtype=np.uint32, voxel_size=voxel_size
                ),
                radius=LABEL_RADIUS,
            )
            + RasterizeSkeleton(
                points=swc_keys[1],
                array=raw_keys[1],
                array_spec=ArraySpec(
                    interpolatable=False, dtype=np.uint32, voxel_size=voxel_size
                ),
                radius=RAW_RADIUS,
            )
        )
        data_sources = tuple([data_sources_a, data_sources_b]) + MergeProvider()

        pipeline = data_sources + FusionAugment(
            raw_keys[0],
            raw_keys[1],
            labels_keys[0],
            labels_keys[1],
            fused,
            fused_labels,
            blend_mode="intensity",
            blend_smoothness=BLEND_SMOOTHNESS,
            num_blended_objects=0,
        )

        with build(pipeline):
            batch = pipeline.request_batch(request)

        fused_data = batch[fused].data
        fused_data = np.pad(fused_data, (1,), "constant", constant_values=(0,))

        a_data = batch[raw_keys[0]].data
        a_data = np.pad(a_data, (1,), "constant", constant_values=(0,))

        b_data = batch[raw_keys[1]].data
        b_data = np.pad(b_data, (1,), "constant", constant_values=(0,))

        diff = np.linalg.norm(fused_data - a_data - b_data)
        self.assertAlmostEqual(diff, 0)

    def test_two_disjoint_lines_softmask(self):
        LABEL_RADIUS = 3
        RAW_RADIUS = 3
        # exagerated to show problem
        BLEND_SMOOTHNESS = 10

        bb = Roi(Coordinate([0, 0, 0]), ([256, 256, 256]))
        voxel_size = Coordinate([1, 1, 1])
        swc_files = ("test_line_a.swc", "test_line_b.swc")
        swc_paths = tuple(Path(self.path_to(file_name)) for file_name in swc_files)

        # create two lines seperated by a given distance and write them to swc files
        intercepts, slopes = self._get_line_pair(roi=bb, dist=3 * LABEL_RADIUS)
        for intercept, slope, swc_path in zip(intercepts, slopes, swc_paths):
            swc_points = self._get_points(intercept, slope, bb)
            self._write_swc(swc_path, swc_points)

        # create swc sources
        fused = ArrayKey("FUSED")
        fused_labels = ArrayKey("FUSED_LABELS")
        swc_key_names = ("SWC_A", "SWC_B")
        labels_key_names = ("LABELS_A", "LABELS_B")
        raw_key_names = ("RAW_A", "RAW_B")

        swc_keys = tuple(PointsKey(name) for name in swc_key_names)
        labels_keys = tuple(ArrayKey(name) for name in labels_key_names)
        raw_keys = tuple(ArrayKey(name) for name in raw_key_names)

        # add request
        request = BatchRequest()
        request.add(fused, bb.get_shape())
        request.add(fused_labels, bb.get_shape())
        request.add(labels_keys[0], bb.get_shape())
        request.add(labels_keys[1], bb.get_shape())
        request.add(raw_keys[0], bb.get_shape())
        request.add(raw_keys[1], bb.get_shape())
        request.add(swc_keys[0], bb.get_shape())
        request.add(swc_keys[1], bb.get_shape())

        # data source for swc a
        data_sources_a = tuple()
        data_sources_a = (
            data_sources_a
            + SwcFileSource(swc_paths[0], swc_keys[0], PointsSpec(roi=bb))
            + RasterizeSkeleton(
                points=swc_keys[0],
                array=labels_keys[0],
                array_spec=ArraySpec(
                    interpolatable=False, dtype=np.uint32, voxel_size=voxel_size
                ),
                radius=LABEL_RADIUS,
            )
            + RasterizeSkeleton(
                points=swc_keys[0],
                array=raw_keys[0],
                array_spec=ArraySpec(
                    interpolatable=False, dtype=np.uint32, voxel_size=voxel_size
                ),
                radius=RAW_RADIUS,
            )
        )

        # data source for swc b
        data_sources_b = tuple()
        data_sources_b = (
            data_sources_b
            + SwcFileSource(swc_paths[1], swc_keys[1], PointsSpec(roi=bb))
            + RasterizeSkeleton(
                points=swc_keys[1],
                array=labels_keys[1],
                array_spec=ArraySpec(
                    interpolatable=False, dtype=np.uint32, voxel_size=voxel_size
                ),
                radius=LABEL_RADIUS,
            )
            + RasterizeSkeleton(
                points=swc_keys[1],
                array=raw_keys[1],
                array_spec=ArraySpec(
                    interpolatable=False, dtype=np.uint32, voxel_size=voxel_size
                ),
                radius=RAW_RADIUS,
            )
        )
        data_sources = tuple([data_sources_a, data_sources_b]) + MergeProvider()

        pipeline = data_sources + FusionAugment(
            raw_keys[0],
            raw_keys[1],
            labels_keys[0],
            labels_keys[1],
            fused,
            fused_labels,
            blend_mode="labels_mask",
            blend_smoothness=BLEND_SMOOTHNESS,
            num_blended_objects=0,
        )

        with build(pipeline):
            batch = pipeline.request_batch(request)

        fused_data = batch[fused].data
        fused_data = np.pad(fused_data, (1,), "constant", constant_values=(0,))

        a_data = batch[raw_keys[0]].data
        a_data = np.pad(a_data, (1,), "constant", constant_values=(0,))

        b_data = batch[raw_keys[1]].data
        b_data = np.pad(b_data, (1,), "constant", constant_values=(0,))

        all_data = np.zeros((5,) + fused_data.shape)
        all_data[0, :, :, :] = fused_data
        all_data[1, :, :, :] = a_data + b_data
        all_data[2, :, :, :] = fused_data - a_data - b_data
        all_data[3, :, :, :] = a_data
        all_data[4, :, :, :] = b_data

        # Uncomment to visualize problem
        if imported_volshow:
            volshow(all_data)
            # input("Press enter when you are done viewing the data: ")

        diff = np.linalg.norm(fused_data - a_data - b_data)
        self.assertAlmostEqual(diff, 0)


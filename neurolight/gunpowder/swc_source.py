import numpy as np
from gunpowder import *
from gunpowder.profiling import Timing
import h5py

logger = logging.getLogger(__name__)


class SwcPoint(Point):

    def __init__(self, location, point_id, parent_id, label_id):

        super(SwcPoint, self).__init__(location)

        self.thaw()
        self.point_id = point_id
        self.parent_id = parent_id
        self.label_id = label_id
        self.freeze()

    def copy(self):
        return SwcPoint(self.location, self.point_id, self.parent_id, self.label_id)


class SwcSource(BatchProvider):
    """Read points of a skeleton from a hdf dataset.
    --> todo: should also be possible to read from swc file directly with considering offset and resolution

    Each line in the file represents one point as::

        point_id, structure identifier (soma, axon, ...), x, y, z, radius, parent_id

    where ``parent_id`` can be -1 to indicate no parent.

    Args:

        filename (``string``):

            The HDF5 file.

        dataset (``string``):

            Array key to dataset names that this source offers.

        points (``tuple`` of :class:`PointsKey`):

            The key of the points set to create.

        point_specs (``dict``, :class:`PointsKey`, optional):

            An optional dictionary of point keys to point specs to overwrite
            the points specs automatically determined from the data file.

        scale (scalar or array-like, optional):

            An optional scaling to apply to the coordinates of the points. This
            is useful if the points refer to voxel positions to convert them to
            world units.
    """

    def __init__(self, filename, dataset, points, point_specs=None, scale=None):

        self.filename = filename
        self.dataset = dataset
        self.points = points
        self.point_specs = point_specs
        self.scale = scale

        # variables to keep track of swc skeleton graphs
        self.ndims = 3
        self.data = None
        self.child_to_parent = None
        self.parent_to_children = None
        self.sources = None

    def setup(self):

        self._read_points()

        if self.point_specs is not None:

            assert len(self.point_specs) == len(self.points), 'Number of point keys and point specs differ!'

            for points_key, points_spec in zip(self.points, self.point_specs):
                self.provides(points_key, points_spec)

        else:

            min_bb = Coordinate(np.floor(np.amin(self.data[:, :self.ndims], 0)))
            max_bb = Coordinate(np.ceil(np.amax(self.data[:, :self.ndims], 0)) + 1)
            roi = Roi(min_bb, max_bb - min_bb)

            for points_key in self.points:
                self.provides(points_key, PointsSpec(roi=roi))

    def provide(self, request):

        timing = Timing(self)
        timing.start()

        batch = Batch()

        for points_key in self.points:

            if points_key not in request:
                continue

            # get points for output size / center region
            min_bb = request[points_key].roi.get_begin()
            max_bb = request[points_key].roi.get_end() - Coordinate([1,1,1])

            logger.debug(
                "SWC points source got request for %s",
                request[points_key].roi)

            point_filter = np.ones((self.data.shape[0],), dtype=np.bool)
            for d in range(self.ndims):
                point_filter = np.logical_and(point_filter, self.data[:, d] >= min_bb[d])
                point_filter = np.logical_and(point_filter, self.data[:, d] <= max_bb[d])

            points_data = self._get_points(point_filter)
            points_spec = PointsSpec(roi=request[points_key].roi.copy())
            relatives_resampled = {}

            # in order to draw skeleton in the entire roi, get parent and children and resample them
            # to be at the edge of the roi, add them to points_data
            # create new SwcPoint(location, point_id, parent_id, label_id)
            if len(points_data) < self.data.shape[0]:

                for point_id in points_data:
                    point = points_data[point_id]

                    # processing parent node
                    if point_id not in self.sources:
                        parent_id = self.child_to_parent[point_id]

                        if parent_id not in points_data:

                            parent = self._get_point(self.data[:, 3] == parent_id)
                            loc = self._resample_relative(point, parent, min_bb, max_bb)
                            relatives_resampled.update({parent_id: SwcPoint(loc, parent_id, -1, point.label_id)})

                    # processing children
                    if point_id in self.parent_to_children.keys():

                        for child_id in self.parent_to_children[point_id]:

                            if child_id not in points_data:

                                child = self._get_point(self.data[:, 3] == child_id)
                                loc = self._resample_relative(point, child, min_bb, max_bb)
                                relatives_resampled.update({child_id: SwcPoint(loc, child_id, point_id, point.label_id)})

            points_data.update(relatives_resampled)

            batch.points[points_key] = Points(points_data, points_spec)

        timing.stop()
        batch.profiling_stats.add(timing)

        return batch

    def _open_file(self, filename):
        return h5py.File(filename, 'r')

    def _get_points(self, point_filter):

        filtered = self.data[point_filter]
        return {
            int(p[self.ndims]): SwcPoint(
                p[:self.ndims],
                int(p[self.ndims]),
                int(p[self.ndims + 1]),
                int(p[self.ndims + 2])
            )
            for p in filtered
        }

    def _get_point(self, point_filter):

        filtered = self.data[point_filter][0]
        return SwcPoint(
            filtered[:self.ndims],
            int(filtered[self.ndims]),
            int(filtered[self.ndims + 1]),
            int(filtered[self.ndims + 2])
        )

    def _resample_relative(self, p, relative, min_bb, max_bb):

        dist = relative.location - p.location
        s_bb = np.asarray([(np.asarray(min_bb) - p.location) / dist,
                           (np.asarray(max_bb) - p.location) / dist])

        assert np.sum(np.logical_and((s_bb >= 0),(s_bb <= 1))) > 0, \
            ("Cannot resample point between point %p and relative %p, please check!" %p.location, relative.location)

        s = np.min(s_bb[np.logical_and((s_bb >= 0),(s_bb <= 1))])
        return np.floor(p.location + s * dist)

    def _label_skeleton(self, p, label_id):

        while True:

            if p in self.point_to_label:
                raise RuntimeError("Loop detected in skeleton")

            self.point_to_label[p] = label_id

            children = self.parent_to_children.get(p)
            if children is None:
                break

            if len(children) == 1:
                p = children[0]
                continue
            else:
                for child in children:
                    self._label_skeleton(child, label_id)
                break

    def _label_skeletons(self):

        self.child_to_parent = {}
        self.parent_to_children = {}
        self.sources = []
        self.point_to_label = {}

        logger.info("Finding root nodes...")

        # data = [x, y, z, point_id, parent_id, label_id]
        for p in self.data:

            _, _, _, point_id, parent_id, label_id = (int(x) for x in p)

            if point_id == parent_id:
                self.sources.append(point_id)
            else:
                self.child_to_parent[point_id] = parent_id

                if parent_id in self.parent_to_children:
                    self.parent_to_children[parent_id].append(point_id)
                else:
                    self.parent_to_children[parent_id] = [point_id]

        logger.info("Relabelling skeletons...")

        label_id = 1
        for source in self.sources:
            self._label_skeleton(source, label_id)
            label_id += 1

        for point in self.data:
            point_id = point[3]
            point[5] = self.point_to_label[point_id]

    def _read_points(self):

        logger.info("Reading SWC file %s", self.filename)

        with self._open_file(self.filename) as data_file:

            if self.dataset not in data_file:
                raise RuntimeError("%s not in %s" % (self.dataset, self.filename))

            points = data_file[self.dataset]

            # data = [x, y, z, point_id, parent_id, label_id]
            self.data = np.transpose(np.array([points[:, 2], points[:, 3], points[:, 4], points[:, 0], points[:, 6]]))
            self.data = np.concatenate((self.data, np.zeros((self.data.shape[0], 1), dtype=self.data.dtype)), axis=1)

            # separate skeletons and assign labels
            self._label_skeletons()

            resolution = None
            if data_file[self.dataset].attrs.__contains__('resolution'):
                resolution = data_file[self.dataset].attrs.get('resolution')

            if self.scale is not None:
                self.data[:, :self.ndims] *= self.scale
                if resolution is not None:
                    if resolution != self.scale:
                        logger.warning("WARNING: File %s contains resolution information "
                                       "for %s (dataset %s). However, voxel size has been set to scale factor %s." 
                                       "This might not be what you want.",
                                       self.filename, points, self.dataset, self.scale)
            elif resolution is not None:
                self.data[:, :self.ndims] *= resolution
            else:
                logger.warning("WARNING: No scaling factor or resolution information in file %s"
                               "for %s (dataset %s). So points refer to voxel positions, "
                               "this might not be what you want.",
                               self.filename, points, self.dataset)

from collections import OrderedDict
import random
import threading

import numpy as np
import tables

from dl1_data_handler.image_mapper import ImageMapper
from dl1_data_handler.processor import DL1DataProcessor

camera_type = {
    'LST': 'LSTCam',
    'MSTF': 'FlashCam',
    'MSTN': 'NectarCam',
    'MSTS': 'SCTCam',
    'SST1': 'DigiCam',
    'SSTC': 'CHEC',
    'SSTA': 'ASTRICam'
}

def get_camera_type(tel_type):
    return camera_type[tel_type]

class DL1DataReader:

    @staticmethod
    def __synchronized_open_file(*args, **kwargs):
        with threading.Lock() as lock:
            return tables.open_file(*args, **kwargs)

    @staticmethod
    def __synchronized_close_file(*args, **kwargs):
        with threading.Lock() as lock:
            return self.close(*args, **kwargs)

    def __init__(self,
                 file_list,
                 mode='mono',
                 selected_telescope_type=None,
                 selected_telescope_ids=None,
                 selection_string=None,
                 event_selection=None,
                 image_selection=None,
                 shuffle=False,
                 seed=None,
                 image_channels=None,
                 mapping_method=None,
                 mapping_settings=None,
                 array_info=None,
                 event_info=None,
                 transforms=None,
                 validate_processor=False
                ):

        # Construct dict of filename:file_handle pairs
        self.files = OrderedDict()
        for filename in file_list:
            self.files[filename] = \
                self.__synchronized_open_file(filename, mode='r')

        # Set data loading mode
        # Mono: single images of one telescope type
        # Stereo: events of one telescope type
        # Multi-stereo: events including multiple telescope types
        if mode in ['mono', 'stereo', 'multi-stereo']:
            self.mode = mode
        else:
            raise ValueError("Invalid mode selection '{}'. Valid options: "
                             "'mono', 'stereo', 'multi-stereo'".format(mode))

        self.example_identifiers = None
        self.telescopes = {}
        if selected_telescope_ids is None:
            selected_telescope_ids = {}

        if event_selection is None:
            event_selection = {}

        if image_selection is None:
            image_selection = {}

        if mapping_settings is None:
            mapping_settings = {}

        # Loop over the files to assemble the selected event identifiers
        for filename, f in self.files.items():
            example_identifiers = []

            # Get dict of all the tel_types in the file mapped to their tel_ids
            telescopes = {}
            for row in f.root.Array_Info:
                tel_type = row['tel_type'].decode()
                if tel_type not in telescopes:
                    telescopes[tel_type] = []
                telescopes[tel_type].append(row['tel_id'])

            # Enforce an automatic minimal telescope selection cut:
            # there must be at least one triggered telescope of a
            # selected type in the event
            # Users can include stricter cuts in the selection string
            if self.mode in ['mono', 'stereo']:
                if selected_telescope_type is None:
                    # Default: use the first tel type in the file
                    default = f.root.Array_Info[0]['tel_type'].decode()
                    selected_telescope_type = default
                self.tel_type = selected_telescope_type
                selected_tel_types = [selected_telescope_type]
            elif self.mode == 'multi-stereo':
                if selected_telescope_type is None:
                    # Default: use all tel types
                    selected_telescope_type = list(telescopes)
                self.tel_type = None
                selected_tel_types = selected_telescope_type
            # Multiplicity condition not implemented (pytables won't take a numpy
            # function as a condition

            #multiplicity_conditions = ['np.count_nonzero(' + tel_type + '_indices) > 0'
            #                           for tel_type in selected_tel_types]
            #tel_cut_string = '(' + ' | '.join(multiplicity_conditions) + ')'

            # Combine minimal telescope cut with explicit selection cuts
            if selection_string:
                cut_condition = selection_string
#                cut_condition = selection_string + ' & ' + tel_cut_string
            else:
            #    cut_condition = tel_cut_string
                cut_condition = 'mc_energy > 0'

            # Select which telescopes from the full dataset to include in each
            # event by a telescope type and an optional list of telescope ids.
            selected_telescopes = {}
            for tel_type in selected_tel_types:
                available_tel_ids = telescopes[tel_type]
                # Keep only the selected tel ids for the tel type
                if tel_type in selected_telescope_ids:
                    # Check all requested telescopes are available to select
                    requested_tel_ids = selected_telescope_ids[tel_type]
                    invalid_tel_ids = (set(requested_tel_ids)
                                       - set(available_tel_ids))
                    if invalid_tel_ids:
                        raise ValueError("Tel ids {} are not a valid selection"
                                         "for tel type '{}'".format(
                                             invalid_tel_ids, tel_type))
                    selected_telescopes[tel_type] = requested_tel_ids
                else:
                    selected_telescopes[tel_type] = available_tel_ids

            selected_nrows = set([row.nrow for row
                              in f.root.Event_Info.where(cut_condition)])
            selected_nrows &= self._select_event(f, event_selection)
            selected_nrows = list(selected_nrows)

            # Make list of identifiers of all examples passing event selection
            if self.mode in ['stereo', 'multi-stereo']:
                example_identifiers = [(filename, nrow) for nrow
                                       in selected_nrows]
            elif self.mode == 'mono':
                example_identifiers = []
                field = '{}_indices'.format(self.tel_type)
                selected_indices = f.root.Event_Info.read_coordinates(selected_nrows, field=field)
                for tel_id in selected_telescopes[self.tel_type]:
                    img_ids = set(selected_indices[:, telescopes[self.tel_type].index(tel_id)])
                    img_ids.remove(0)
                    img_ids = list(img_ids)
                    # TODO handle all selected channels
                    mask = self._select_image(f.root[self.tel_type][img_ids]['image_charge'], image_selection)
                    img_ids = np.array(img_ids)[mask]
                    for index in img_ids:
                            example_identifiers.append((filename, index, tel_id))

            # Confirm that the files are consistent and merge them
            if not self.telescopes:
                self.telescopes = telescopes
            if self.telescopes != telescopes:
                raise ValueError("Inconsistent telescope definition in "
                                 "{}".format(filename))
            self.selected_telescopes = selected_telescopes

            if self.example_identifiers is None:
                self.example_identifiers = example_identifiers
            else:
                self.example_identifiers.extend(example_identifiers)

        # Shuffle the examples
        if shuffle:
            random.seed(seed)
            random.shuffle(self.example_identifiers)

        if image_channels is None:
            image_channels = ['image_charge']
        self.image_channels = image_channels
        self.image_mapper = ImageMapper(channels=self.image_channels,
                                        **mapping_settings)

        if array_info is None:
            array_info = []
        self.array_info = array_info

        if event_info is None:
            event_info = []
        self.event_info = event_info

        # Construct example description (before preprocessing)
        if self.mode == 'mono':
            self.unprocessed_example_description = [
                {
                    'name': 'image',
                    'tel_type': self.tel_type,
                    'base_name': 'image',
                    'shape': self.image_mapper.image_shapes[get_camera_type(self.tel_type)],
                    'dtype': np.dtype(np.float32)
                    }
                ]
            for col_name in self.array_info:
                col = f.root.Array_Info.cols._f_col(col_name)
                self.unprocessed_example_description.append(
                    {
                        'name': col_name,
                        'tel_type': self.tel_type,
                        'base_name': col_name,
                        'shape': (1,) + col.shape[1:],
                        'dtype': col.dtype
                        }
                    )
        elif self.mode == 'stereo':
            num_tels = len(self.selected_telescopes[self.tel_type])
            self.unprocessed_example_description = [
                {
                    'name': 'image',
                    'tel_type': self.tel_type,
                    'base_name': 'image',
                    'shape': ((num_tels,)
                              + self.image_mapper.image_shapes[get_camera_type(self.tel_type)]),
                    'dtype': np.dtype(np.float32)
                    },
                {
                    'name': 'trigger',
                    'tel_type': self.tel_type,
                    'base_name': 'trigger',
                    'shape': (num_tels,),
                    'dtype': np.dtype(np.int8)
                    }
                ]
            for col_name in self.array_info:
                col = f.root.Array_Info.cols._f_col(col_name)
                self.unprocessed_example_description.append(
                    {
                        'name': col_name,
                        'tel_type': self.tel_type,
                        'base_name': col_name,
                        'shape': (num_tels,) + col.shape[1:],
                        'dtype': col.dtype
                        }
                    )
        elif self.mode == 'multi-stereo':
            self.unprocessed_example_description = []
            for tel_type in self.selected_telescopes:
                num_tels = len(self.selected_telescopes[tel_type])
                self.unprocessed_example_description.extend([
                    {
                        'name': tel_type + '_image',
                        'tel_type': tel_type,
                        'base_name': 'image',
                        'shape': ((num_tels,)
                                  + self.image_mapper.image_shapes[get_camera_type(tel_type)]),
                        'dtype': np.dtype(np.float32)
                        },
                    {
                        'name': tel_type + '_trigger',
                        'tel_type': tel_type,
                        'base_name': 'trigger',
                        'shape': (num_tels,),
                        'dtype': np.dtype(np.int8)
                        }
                    ])
                for col_name in self.array_info:
                    col = f.root.Array_Info.cols._f_col(col_name)
                    self.unprocessed_example_description.append(
                        {
                            'name': tel_type + '_' + col_name,
                            'tel_type': tel_type,
                            'base_name': col_name,
                            'shape': (num_tels,) + col.shape[1:],
                            'dtype': col.dtype
                            }
                        )
        # Add event info to description
        for col_name in self.event_info:
            col = f.root.Event_Info.cols._f_col(col_name)
            self.unprocessed_example_description.append(
                {
                    'name': col_name,
                    'tel_type': None,
                    'base_name': col_name,
                    'shape': col.shape[1:],
                    'dtype': col.dtype
                    }
                )

        self.processor = DL1DataProcessor(
            self.mode,
            self.unprocessed_example_description,
            transforms,
            validate_processor
            )

        # Definition of preprocessed example
        self.example_description = self.processor.output_description

    def _select_event(self, file, filters):
        """
        Filter the data event wise.
        Parameters
        ----------
            file (tables.File): the file containing the data
            filters (dict): dictionary of `{filter_function: filter_parameters}` to apply on the data

        Returns
        -------
        the filtered nrows

        """
        indices = set(np.arange(len(file.root.Event_Info[:])))
        for filter_function, filter_parameters in filters.items():
            indices &= filter_function(self, file, **filter_parameters)
        return indices

    def _select_image(self, images, filters):
        """
        Filter the data image wise.
        Parameters
        ----------
            images (tables.File): the images to filter on
            filters (dict): dictionary of `{filter_function: filter_parameters}` to apply on the data

        Returns
        -------
        the mask of filtered images

                """
        mask = np.full(len(images), True)
        for filter_function, filter_parameters in filters.items():
            mask &= filter_function(self, images, **filter_parameters)
        return mask

    # Get a single telescope image from a particular event, uniquely
    # identified by the filename, tel_type, and image table index.
    # First extract a raw 1D vector and transform it into a 2D image using a
    # mapping table. When 'axial addressing' is selected this function should
    # return the unmapped vector.
    def _get_image(self, filename, tel_type, image_index):

        f = self.files[filename]
        record = f.root._f_get_child(tel_type)[image_index]
        query = "tel_type == '{}'".format(tel_type)
        length = [x['num_pixels'] for x
                  in f.root.Telescope_Info.where(query)][0]
        num_channels = len(self.image_channels)
        vector = np.empty(shape=(length, num_channels), dtype=np.float32)
        # If the telescope didn't trigger, the image index is 0 and a blank
        # image of all zeros with be loaded
        for i, channel in enumerate(self.image_channels):
            vector[:, i] = record[channel]
        # If axial addressing is selected, we only need the unmapped vector.
        if self.image_mapper.mapping_method[get_camera_type(tel_type)] == 'axial_addressing':
           return vector
        image = self.image_mapper.map_image(vector, get_camera_type(tel_type))
        return image

    def __len__(self):
        return len(self.example_identifiers)

    def __getitem__(self, idx):

        identifiers = self.example_identifiers[idx]

        # Get record for the event
        filename = identifiers[0]
        f = self.files[filename]

        def append_array_info(array_info, tel_id):
            query = "(tel_id == {}) & (tel_type == {})".format(tel_id,
                    self.tel_type.encode())
            for row in f.root.Array_Info.where(query):
                for info, column in zip(array_info, self.array_info):
                    dtype = f.root.Array_Info.cols._f_col(column).dtype
                    info.append(np.array(row[column], dtype=dtype))

        def load_tel_type_data(nrow, tel_type):
            images = []
            triggers = []
            array_info = [[] for column in self.array_info]
            for tel_id in self.selected_telescopes[tel_type]:
                tel_index = self.telescopes[tel_type].index(tel_id)
                image_index = f.root.Event_Info[nrow][
                    tel_type + '_indices'][tel_index]
                image = self._get_image(filename, tel_type, image_index)
                trigger = 0 if image_index == 0 else 1
                images.append(image)
                triggers.append(trigger)
                append_array_info(array_info, tel_id)
            example = [np.stack(images), np.array(triggers, dtype=np.int8)]
            example.extend([np.stack(info) for info in array_info])
            return example

        # Load the data and any selected array info
        if self.mode == "mono":
            # Get a single image
            image_index, tel_id = identifiers[1:3]
            nrow = f.root._f_get_child(self.tel_type)[image_index]['event_index']

            image = self._get_image(filename, self.tel_type, image_index)
            example = [image]

            array_info = [[] for column in self.array_info]
            append_array_info(array_info, tel_id)
            example.extend([np.stack(info) for info in array_info])
        elif self.mode == "stereo":
            # Get a list of images and an array of binary trigger values
            nrow = identifiers[1]
            example = load_tel_type_data(nrow, self.tel_type)
        elif self.mode == "multi-stereo":
            # Get a list of images and an array of binary trigger values
            # for each selected telescope type
            nrow = identifiers[1]
            example = []
            for tel_type in self.selected_telescopes:
                tel_type_example = load_tel_type_data(nrow, tel_type)
                example.extend(tel_type_example)

        # Load event info
        record = f.root.Event_Info[nrow]
        for column in self.event_info:
            dtype = f.root.Event_Info.cols._f_col(column).dtype
            example.append(np.array(record[column], dtype=dtype))

        # Preprocess the example
        example = self.processor.process(example)

        return example

    # Return a dictionary of number of examples in the dataset, grouped by
    # the array names listed in the iterable group_by.
    # If example_indices is a list of indices, consider only those examples,
    # otherwise all examples in the reader are considered.
    def num_examples(self, group_by=None, example_indices=None):
        grouping_indices = []
        if group_by is not None:
            for name in group_by:
                for idx, des in enumerate(self.example_description):
                    if des['name'] == name:
                        grouping_indices.append(idx)
        group_nums = {}
        if example_indices is None:
            example_indices = list(range(len(self)))
        for idx in example_indices:
            example = self[idx]
            # Use tuple() and tolist() to convert list and NumPy array
            # to hashable keys
            group = tuple([example[idx].tolist() for idx in grouping_indices])
            if group in group_nums:
                group_nums[group] += 1
            else:
                group_nums[group] = 1
        return group_nums

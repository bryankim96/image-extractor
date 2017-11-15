"""
ImageExtractor class for writing processed data from ctapipe containers to HDF5 files.
"""
import random
import argparse
import math
import pickle as pkl
import logging
import glob
import shutil
import os

from configobj import ConfigObj
from validate import Validator
import numpy as np
from astropy import units as u
from tables import *
from ctapipe.io.hessio import hessio_event_source
from ctapipe.calib import pedestals,CameraCalibrator

import config
import trace_converter
import row_descriptors

__all__ = ['ImageExtractor']

logger = logging.getLogger(__name__)

class ImageExtractor:

    NUM_PIXELS = {'LST':1855,'MST':1764,'SCT':11328,'SST':0}
    IMAGE_SHAPE = {'SCT':(120,120)}

    def __init__(self, output_path,bins_cuts_dict,mode,tel_type_mode,storage_mode,
            img_channels,include_timing,format_mode,img_scale_factors,img_dtype,img_dim_order,energy_bins,energy_bin_units,energy_recon_bins):
        
        self.output_path = output_path
        self.bins_cuts_dict = bins_cuts_dict
        
        self.mode = mode
        self.tel_type_mode = tel_type_mode
        self.storage_mode = storage_mode

        self.img_channels = img_channels
        self.include_timing = include_timing
        self.format_mode = format_mode
        self.img_scale_factors = img_scale_factors
        self.img_dtype = img_dtype
        self.img_dim_order = img_dim_order
       
        self.energy_bins = energy_bins
        self.energy_bin_units = energy_bin_units

        self.energy_recon_bins = energy_recon_bins

        self.trace_converter = trace_converter.TraceConverter(self.img_dtype,self.img_dim_order,self.img_channels,self.img_scale_factors)

    @classmethod
    def from_config(cls,output_path,bins_cuts_dict,config):
        
        img_mode = config['image']['mode']

        if img_mode == 'PIXELS_3C':
            img_channels = 3
            include_timing = False
            format_mode = '2d'
        elif img_mode == 'PIXELS_1C':
            img_channels = 1
            include_timing = False
            format_mode = '2d'
        elif img_mode == 'PIXELS_TIMING_2C':
            img_channels = 2
            include_timing = True
            format_mode = '2d'
        elif img_mode == 'PIXELS_TIMING_3C':
            img_channels = 3 
            include_timing = True
            format_mode = '2d'
        elif img_mode == 'VECTOR':
            img_channels = 1
            include_timing = False
            format_mode = '1d'
        elif img_mode == 'VECTOR_TIMING':
            img_channels = 2
            include_timing = True
            format_mode = '1d'
        else:
            logger.error("Invalid image format (img_mode).")
            raise ValueError('Image processing mode not recognized.')

        mode = config['mode']
        tel_type_mode = config['telescope']['type_mode']
        storage_mode = config['storage_mode']
        
        img_scale_factors = {'SCT':config['image']['scale_factor']}
        img_dtype = config['image']['dtype']
        img_dim_order = config['image']['dim_ordering']
        energy_bin_units = config['energy_bins']['units']
        
        use_bins_cuts_dict = config['use_pkl_dict']

        if use_bins_cuts_dict and bins_cuts_dict is None:
            logger.error("Cuts enabled in config file but dictionary missing.")
            raise ValueError("Cuts enabled in config file but dictionary missing.")
            
        #energy bins
        if mode == 'gh_class':

            e_min = float(config['energy_bins']['min'])
            e_max = float(config['energy_bins']['max'])
            e_bin_size = float(config['energy_bins']['bin_size'])
            num_e_bins = int((e_max - e_min)/e_bin_size)
            energy_bins = [(e_min + i*e_bin_size, e_min + (i+1)*e_bin_size) for i in range(num_e_bins)]
            energy_recon_bins = None

        elif mode == 'energy_recon':

            erec_min = float(config['energy_recon']['bins']['min'])
            erec_max = float(config['energy_recon']['bins']['max'])
            erec_bin_size = float(config['energy_recon']['bins']['bin_size'])
            num_erec_bins = int((erec_max - erec_min)/erec_bin_size)
            energy_bins = None
            energy_recon_bins = [(erec_min+i*erec_bin_size,erec_min + (i+1)*erec_bin_size) for i in range(num_erec_bins)]   

        return cls(output_path,bins_cuts_dict,mode,tel_type_mode,storage_mode,img_channels,include_timing,format_mode,img_scale_factors,img_dtype,img_dim_order,energy_bins,energy_bin_units,energy_recon_bins)

    def select_telescopes(self,data_file):
            
        logger.info("Getting telescope types...")

        #collect telescope lists 
        source_temp = hessio_event_source(data_file,max_events=1)

        all_tels = {'SST': [], 'SCT': [], 'MST': [], 'LST': []}

        for event in source_temp: 
            for tel_id in event.inst.telescope_ids:
                if event.inst.num_pixels[tel_id] == self.NUM_PIXELS['SCT']:
                    all_tels['SCT'].append(tel_id)
                elif event.inst.num_pixels[tel_id] == self.NUM_PIXELS['LST']:
                    all_tels['LST'].append(tel_id)
                elif event.inst.num_pixels[tel_id] == self.NUM_PIXELS['MST']:
                    all_tels['MST'].append(tel_id)
                elif event.inst.num_pixels[tel_id] == self.NUM_PIXELS['SST']:
                    all_tels['SST'].append(tel_id)
                else:
                    logger.error("Unknown telescope type (invalid num_pixels).")
                    raise ValueError("Unknown telescope type (invalid num_pixels: {}).".format(event.inst.num_pixels[tel_id]))

        #select telescopes by type
        logger.info("Telescope Mode: ",self.tel_type_mode)

        if self.tel_type_mode == 'SST':
            selected_tel_types = ['SST']
        elif self.tel_type_mode == 'SCT':
            selected_tel_types = ['SCT']
        elif self.tel_type_mode == 'LST':
            selected_tel_types = ['LST']
        elif self.tel_type_mode == 'SCT+LST':
            selected_tel_types = ['SCT','LST']
        elif self.tel_type_mode == 'SST+SCT':
            selected_tel_types = ['SST','SCT']
        elif self.tel_type_mode == 'SST+LST':
            selected_tel_types = ['SST','LST']
        elif self.tel_type_mode == 'ALL':
            selected_tel_types = ['SST','SCT','LST']
        else:
            logger.error("Telescope selection mode invalid.")
            raise ValueError('Telescope selection mode not recognized.') 

        selected_tels = {key:all_tels[key] for key in selected_tel_types}

        num_tel = 0

        for tel_type in selected_tels.keys():
            logger.info(tel_type + ": " + str(len(selected_tels[tel_type])) + " out of " + str(len(all_tels[tel_type])) + " telescopes selected.")
            num_tel += len(selected_tels[tel_type])

        return selected_tels,num_tel

    def process_data(self, data_file, max_events):
        """
        Function to read and write data from ctapipe containers to HDF5 
        """

        logger.info("Mode: ",self.mode)
        logger.info("File storage mode: ",self.storage_mode)
        logger.info("Image scale factors: ",self.img_scale_factors)
        logger.info("Image array type: ",self.img_dtype)
        logger.info("Image dim order: ",self.img_dim_order)

        logger.info("Preparing HDF5 file structure...")

        f = open_file(self.output_path, mode = "a", title = "Output File") 

        #create groups for each energy bin
        # (Future flattening) if self.mode == 'gh_class' and self.bins_cuts_dict is not None:
        if self.mode == 'gh_class':
            #create groups for each energy bin (naming convention = "E[NUM_BIN]")
            groups = []
            for i in range(len(self.energy_bins)):
                #create group if it doesn't already exist
                if not f.__contains__("/E" + str(i)):
                    group = f.create_group("/", "E" + str(i), 'Energy bin group' + str(i))
                    group._v_attrs.min_energy = self.energy_bins[i][0]
                    group._v_attrs.max_energy = self.energy_bins[i][1]
                    group._v_attrs.units = self.energy_bin_units
                group = eval('f.root.E{}'.format(str(i)))
                groups.append(group)
        # (Future flattening) else:
        elif self.mode == 'energy_recon':
            groups = [f.root]

        #read and select telescopes
        selected_tels, num_tel = self.select_telescopes(data_file)

        #create single table in root group for telescope information
        if not f.__contains__('/Tel_Table'):
            tel_pos_table = f.create_table("/",'Tel_Table', row_descriptors.Tel, "Table of telescope ids, positions, and types")
            tel_row = tel_pos_table.row

            source_temp = hessio_event_source(data_file,max_events=1)
            for event in source_temp: 
                for tel_type in selected_tels.keys():
                    for tel_id in selected_tels[tel_type]:
                        tel_row["tel_id"] = tel_id
                        tel_row["tel_x"] = event.inst.tel_pos[tel_id].value[0]
                        tel_row["tel_y"] = event.inst.tel_pos[tel_id].value[1]
                        tel_row["tel_z"] = event.inst.tel_pos[tel_id].value[2]
                        tel_row["tel_type"] = tel_type
                        tel_row["run_array_direction"] = event.mcheader.run_array_direction
                        tel_row.append()

        #create event table + tel arrays in each group
        for group in groups:
            if not group.__contains__('Events'):
                table = f.create_table(group, 'Events', row_descriptors.Event, "Table of Events") 
                descr = table.description._v_colobjects
                descr2 = descr.copy()

                if self.mode == 'energy_recon':
                    descr2['energy_reconstruction_bin_label'] = UInt8Col()

                if self.storage_mode == 'all':
                    descr2["trig_list"] = UInt8Col(shape=(num_tel)) 
                elif self.storage_mode == 'mapped':
                    descr2["tel_map"] = Int32Col(shape=(num_tel))
        
                for tel_type in selected_tels.keys():
                    if tel_type == 'SST':
                        if self.format_mode == '2d':
                            img_width = self.IMAGE_SHAPE['SST'][0]*self.img_scale_factors['SST']
                            img_length = self.IMAGE_SHAPE['SST'][1]*self.img_scale_factors['SST']
                        num_pixels = self.NUM_PIXELS['SST']
                    elif tel_type == 'SCT':
                        if self.format_mode == '2d':
                            img_width = self.IMAGE_SHAPE['SCT'][0]*self.img_scale_factors['SCT']
                            img_length = self.IMAGE_SHAPE['SCT'][1]*self.img_scale_factors['SCT']
                        num_pixels = self.NUM_PIXELS['SCT']
                    elif tel_type == 'LST':
                        if self.format_mode == '2d':
                            img_width = self.IMAGE_SHAPE['LST'][0]*self.img_scale_factors['LST']
                            img_length = self.IMAGE_SHAPE['LST'][1]*self.img_scale_factors['LST']
                        num_pixels = self.NUM_PIXELS['LST']

                    #for all storage, add columns to event table for each telescope
                    for tel_id in selected_tels[tel_type]:
                        if self.storage_mode == 'all':
                            if self.format_mode == '2d':
                                descr2["T" + str(tel_id)] = UInt16Col(shape=(img_width, img_length, self.img_channels))
                            elif self.format_mode == '1d':
                                descr2["T" + str(tel_id)] = UInt16Col(shape=(num_pixels,self.img_channels))
                        elif self.storage_mode == 'mapped':
                            if not group.__contains__('T'+str(tel_id)):
                                if self.format_mode == '2d':
                                    array = f.create_earray(group, 'T' + str(tel_id), Int16Atom(), (0, img_width, img_length, self.img_channels))
                                elif self.format_mode == '1d':
                                    array = f.create_earray(group, 'T' + str(tel_id), Int16Atom(), (0, num_pixels, self.img_channels))


                table2 = f.create_table(group, 'temp', descr2, "Table of Events")
                table.attrs._f_copy(table2)
                table.remove()
                table2.move(group, 'Events')
            
        #specify calibration and other processing options
        cal = CameraCalibrator(None,None)

        logger.info("Processing events...")

        event_count = 0
        passing_count = 0

        source = hessio_event_source(data_file,allowed_tels=[j for i in selected_tels.keys() for j in selected_tels[i]], max_events=max_events)
        
        for event in source:
            event_count += 1

            #get energy bin and reconstructed energy
            if self.bins_cuts_dict is not None:
                if (event.r0.run_id,event.r0.event_id) in self.bins_cuts_dict:
                    bin_number, reconstructed_energy = self.bins_cuts_dict[(event.r0.run_id,event.r0.event_id)]
                    passing_count +=1
                else:
                    continue
            else:
                #if pass cuts (applied locally):
                bin_number, reconstructed_energy = [0, 0]
                #else:
                #continue

            #calibrate raw image (charge extraction + pedestal subtraction + trace integration)
            #NOTE: MUST BE MOVED UP ONCE ENERGY RECONSTRUCTION AND BIN NUMBERS ARE CALCULATED LOCALLY
            cal.calibrate(event)

            #compute energy reconstruction bin true label
            if self.mode == 'energy_recon':
                for i in range(len(self.energy_recon_bins)):
                    if event.mc.energy.value >= 10**(self.energy_recon_bins[i][0]) and event.mc.energy.value < 10**(self.energy_recon_bins[i][1]):
                        erec_bin_label = i
                        break

            if self.mode == 'energy_recon':
                table = eval('f.root.Events')
            elif self.mode == 'gh_class':
# (Future flattening) if self.bins_cuts_dict is not None:
                table = eval('f.root.E{}.Events'.format(str(bin_number)))
#                else:
#                    table = eval('f.root.Events'.format(str(bin_number)))

            event_row = table.row

            if self.storage_mode == 'all':
                trig_list = []
            elif self.storage_mode == 'mapped':
                tel_map = []
            
            for tel_type in selected_tels.keys():
                for tel_id in selected_tels[tel_type]:
                    if tel_id in event.r0.tels_with_data:
                        pixel_vector = event.dl1.tel[tel_id].image
                        #truncate at 0, scale by 100, round
                        pixel_vector[pixel_vector < 0] = 0
                        pixel_vector = [round(i*100) for i in pixel_vector[0]]

                        if self.include_timing:
                            peaks_vector = event.dl1.tel[tel_id].peakpos[0]
                        else:
                            peaks_vector = None 

                        if self.format_mode == '2d':
                            image_array = self.trace_converter.convert_SCT(pixel_vector, peaks_vector)
                        elif self.format_mode == '1d':
                            image_array = np.column_stack((pixel_vector, peaks_vector))
                        logger.debug('image_array shape: '+format(str(image_array.shape)))
                        logger.debug('image_array exp. shape: '+format(str(np.expand_dims(image_array, axis=0).shape)))

                        if self.storage_mode == 'all':
                            trig_list.append(1)
                            event_row["T" + str(tel_id)] = image_array
                        elif self.storage_mode == 'mapped':
                            #(Future flattening) array = eval('f.root.T{}'.format(tel_id))
                            array = eval('f.root.E{}.T{}'.format(bin_number, tel_id))
                            next_index = array.nrows
                            logger.debug('earray shape: '+format(str(array.shape)))
                            array.append(np.expand_dims(image_array, axis=0))
                            tel_map.append(next_index)

                    else:
                        if self.storage_mode == 'all':
                            trig_list.append(0)
                            event_row["T" + str(tel_id)] = self.trace_converter.convert_SCT(None,None)
                        elif self.storage_mode == 'mapped':
                            tel_map.append(-1)
                            
            if self.storage_mode == 'all':
                event_row['trig_list'] = trig_list 
            elif self.storage_mode == 'mapped':
                event_row['tel_map'] = tel_map

            event_row['event_number'] = event.r0.event_id
            event_row['run_number'] = event.r0.run_id
            event_row['gamma_hadron_label'] = event.mc.shower_primary_id
            event_row['core_x'] = event.mc.core_x.value
            event_row['core_y'] = event.mc.core_y.value
            event_row['h_first_int'] = event.mc.h_first_int.value
            event_row['mc_energy'] = event.mc.energy.value
            event_row['alt'] = event.mc.alt.value
            event_row['az'] = event.mc.az.value
            event_row['reconstructed_energy'] = reconstructed_energy

            if self.mode == 'energy_recon':
                event_row['energy_reconstruction_bin_label'] = erec_bin_label

            event_row.append()

            table.flush()

        logger.info("{} events processed".format(event_count))
        logger.info("{} events passed cuts/written to file".format(passing_count))
        logger.info("Done!")

    def shuffle_data(self,h5_file):

        #open input hdf5 file
        f = open_file(h5_file, mode = "r+", title = "Input file")

        if self.mode == 'gh_class':
            for group in f.walk_groups("/"):
                if not group == f.root:
                    #copy event table, but shuffle
                    table = group.Events
                    descr = table.description

                    num_events = table.shape[0]
                    new_indices = [i for i in range(num_events)]
                    random.shuffle(new_indices)

                    table_new = f.create_table(group,'Events_temp', descr,"Table of events")

                    for i in range(num_events):
                        table_new.append([tuple(table[new_indices[i]])]) 

                    table.remove()
                    table_new.move(group, 'Events')
            
    def split_data(self,h5_file,split_dict):

        split_sum = 0
        for i in split_dict:
            split_sum += split_dict[i]
        assert split_sum == 1,"Split fractions do not add up to 1"

        #open input hdf5 file
        f = open_file(h5_file, mode = "r+", title = "Input file")

        tables = []
        new_tables = []
   
        if self.mode == 'gh_class':
            for group in f.walk_groups("/"):
                if not group == f.root:
                    #copy events into separate tables
                    table = group.Events
                    descr = table.description

                    num_events = table.shape[0]
                    indices = range(num_events)
                    i = 0

                    for split in list(split_dict.keys()):
                        table_new = f.create_table(group, 'Events_' + split, descr, "Table of " + split + " Events")

                        split_fraction = split_dict[split]
                        
                        if i+int(split_fraction*num_events) <= num_events:
                            split_indices = indices[i:i+int(split_fraction*num_events)]
                        else:
                            split_indices = indices[i:num_events]

                        for j in split_indices:
                            table_new.append([tuple(table[j])])

                        i += int(split_fraction*num_events)

                    table.remove()

if __name__ == '__main__':
    
    parser = argparse.ArgumentParser(description='Load image data and event parameters from a simtel file into a formatted HDF5 file.')
    parser.add_argument('data_files', help='wildcard path to input .simtel files')
    parser.add_argument('hdf5_path', help='path of output HDF5 file, or currently existing file to append to')
    parser.add_argument('config_file',help='configuration file specifying the selected telescope ids from simtel file, the desired energy bins, the correst image output dimensions/dtype, ')
    parser.add_argument('--bins_cuts_dict_file',help='path of .pkl file containing bins/cuts dictionary')
    parser.add_argument("--debug", help="print debug/logger messages", action="store_true")
    parser.add_argument("--max_events", help="set a maximum number of events to process from each file",type=int)
    parser.add_argument("--shuffle",help="shuffle output data file", action="store_true")
    parser.add_argument("--split",help="split output data file into separate event tables", action="store_true")

    args = parser.parse_args()

    #Configuration file, load + validate
    spc = config.config_spec.split('\n')
    config = ConfigObj(args.config_file,configspec=spc)
    validator = Validator()
    val_result = config.validate(validator)

    #load bins cuts dictionary from file
    if args.bins_cuts_dict_file is not None:
        bins_cuts_dict = pkl.load(open(args.bins_cuts_dict_file, "rb" ))
    else:
        bins_cuts_dict = None

    extractor = ImageExtractor.from_config(args.hdf5_path,bins_cuts_dict,config)

    data_files = glob.glob(args.data_files)

    for data_file in data_files:
        extractor.process_data(data_file,args.max_events)

    if args.shuffle:
        extractor.shuffle_data(args.hdf5_path)

    split_dict = {'Training':0.9,'Validation':0.1}

    if args.split:
        extractor.split_data(args.hdf5_path,split_dict)



#!/usr/bin/env python3
import sys
import os
import struct
import logging
import subprocess
import plistlib
import re

try:
    from progressbar import FileTransferSpeed, ProgressBar, Percentage, Bar
except:
    pass

try:
    from appdirs import user_data_dir
    data_dir = user_data_dir('sky3ds', 'Aperture Laboratories')
except:
    pass

from sky3ds import gamecard, titles

class Sky3DS_Disk:
    """This class can manage a sdcard for sky3ds"""

    diskfp = None
    disk_size = None
    disk_path = None

    is_sky3ds_disk = False

    rom_list = []
    free_blocks = []

    def __init__(self, disk_path, diskfp=None, disk_size=None):
        """Keyword Arguments:

        disk_path -- Location to sdcard blockdevice (not mount or partition!)"""

        self.disk_path = disk_path

        if diskfp and disk_size:
            self.diskfp = diskfp
            self.disk_size = disk_size

        else:
            try:
                self.diskfp = open(disk_path, "r+b")
            except:
                raise Exception("Couldn't open disk, can't continue.")

            try:
                self.get_disk_size()
            except:
                raise Exception("Couldn't get disksize, will not continue.")

        self.check_if_sky3ds_disk()

        if self.is_sky3ds_disk:
            self.update_rom_list()

    def __del__(self):
        if self.diskfp:
            self.diskfp.close()

    def fail_on_non_sky3ds(self):
        """Fail if disk is not formatted. This is just a sanity function."""

        if not self.is_sky3ds_disk:
            raise Exception("Disk is not formatted, won't continue.")

    def check_if_sky3ds_disk(self):
        """Check if disk is actually a sky3ds sdcard

        This code looks for the "ROMS" string at 0x100."""
        self.diskfp.seek(0x100)
        disk_data = self.diskfp.read(0x4)
        self.is_sky3ds_disk = (b'ROMS' == disk_data)

    def get_disk_size(self):
        """Get sdcard size in bytes

        This currently is an ugly workaround. It seeks to the end of the sdcard
        and reads how many bytes were skipped. This should be replaced with
        something more clean."""

        if sys.platform == 'darwin':
            # meh
            if not re.match("^\/dev\/disk[0-9]+$", self.disk_path):
                raise Exception("Disk path must be in format /dev/diskN")

            try:
                diskname = os.path.basename(self.disk_path)
                diskutil_output = subprocess.check_output(["diskutil", "list", "-plist", self.disk_path])
                if sys.version_info.major == 3:
                    diskutil_plist = plistlib.loads(diskutil_output)
                else:
                    diskutil_plist = plistlib.readPlistFromString(diskutil_output)

                disk_plist = diskutil_plist['AllDisksAndPartitions'][0]

                if not disk_plist['DeviceIdentifier'] == diskname:
                    raise Exception("DeviceIdentifier doesn't match, won't continue.")

                self.disk_size = disk_plist['Size']

            except Exception as e:
                raise Exception("Can't get disk size from diskutil :(\nError was: %s" % e)

        else:
            self.diskfp.seek(0, os.SEEK_END)
            disk_size = self.diskfp.tell()
            disk_size = disk_size - disk_size % 0x2000000
            if disk_size == 0:
                raise Exception("0 byte disk?!")
            self.disk_size = disk_size

    def format(self):
        """Format sdcard

        This code basically fills the first 0x200 bytes with 0xff, except
        at 0x100 - 0x103 where the magic string "ROMS" is written.
        It also writes zeros to the area for Card1 savegames."""

        self.diskfp.seek(0)
        # fill first 0x200 bytes with 0xff except for magic string
        self.diskfp.write(bytearray([0xff]*0x100))
        self.diskfp.write(bytearray("ROMS", "ascii"))
        self.diskfp.write(bytearray([0xff]*0xfc))

        # erase savegame slots
        for i in range(1, 32):
            self.diskfp.seek(i * 0x100000)
            self.diskfp.write(bytearray([0xff] * 0x100000))

        os.fsync(self.diskfp)

        self.check_if_sky3ds_disk()
        self.update_rom_list()

    def update_rom_list(self):
        """Read positions/sizes of roms in bytes and calculate regions of free blocks

        This code basically looks at the first 0x100 bytes of the sdcard
        where sky3ds stores the positions and sizes of roms in 2x4 bytes
        each. The first byte is the position of the rom, the second is
        the size of the rom. Both parameters are in 512-byte sectors.

        Since the rom position headers are not in order and there can be gaps
        this function creates a "map" in which it marks 32MB blocks that are used
        and then look for unmarked blocks. Sky3DS could in theory load roms that
        are not multiples of 32MB, but since all roms seem to be that way there
        is no point to waste the time and ressources to work with 512B sectors here."""

        self.fail_on_non_sky3ds()

        self.diskfp.seek(0)
        position_header_length = 0x100
        raw_positions = self.diskfp.read(position_header_length)
        positions = []
        for i in range(0, int(position_header_length / 8)):
            position = struct.unpack("ii", raw_positions[i*8:i*8+8])
            if position[0] > 0 and position[1] > 0:
                positions += [[len(positions)] + [i*512 for i in position]]

        self.rom_list = positions

        # this function uses 32MB blocks instead of 512B sectors
        # to improve performance (a lot!)

        max_blocks = int(self.disk_size / 0x2000000)

        # create a map like ['X', ' ', ' ', 'X', 'X']
        # where 'X' is used space and ' ' is free space
        block_map = ['X'] + [' ']*(max_blocks-0x1)
        for rom in self.rom_list:
            start = int(rom[1] / 0x2000000)
            size = int(rom[2] / 0x2000000)
            end = start + size
            for i in range(start, end):
                block_map[i] = 'X'

        # inside the map find sequences of ' ' (free space)
        free_blocks = []
        start_block = 0
        i = 0
        for block in block_map:
            if block == ' ' and start_block == 0:
                start_block = i
            elif block == 'X' and not start_block == 0:
                free_blocks += [[ start_block, i - start_block ]]
                start_block = 0

            i+=1
        if not start_block == 0:
            free_blocks += [[ start_block, i - start_block ]]

        # sort sequences of free space by length (descending, useful for later stuff)
        free_blocks = sorted(free_blocks, key=lambda x: x[1], reverse=True)
        free_blocks = [[i*0x10000,j*0x10000] for i,j in free_blocks]

        self.free_blocks = free_blocks

    ################
    # Rom Handling #
    ################

    def ncsd_header(self, slot):
        """Retrieve NCSD header from rom on sdcard.

        This function retrieves the ncsd header from the specified rom on sdcard."""

        self.fail_on_non_sky3ds()

        self.diskfp.seek(self.rom_list[slot][1])
        return gamecard.ncsd_header(self.diskfp.read(0x1200))

    def sky3ds_header(self, slot):
        """Retrieve sky3ds specific header from rom on sdcard.

        This function retrieves the data that was written from template.txt to the sdcard"""

        self.fail_on_non_sky3ds()

        self.diskfp.seek(self.rom_list[slot][1] + 0x1400)
        return bytearray(self.diskfp.read(0x200))

    def write_rom(self, rom, silent=False, progress=None, use_header_bin=False, verbose=False):
        """Write rom to sdcard.

        Roms are stored at the position marked in the position headers (starting
        at 0x2000000).

        This code first looks for a free block with enough space to hold the
        specified rom, then continues to write the data to that location.
        After successful writing the savegame slot for this game is filled with
        zero.
        The last thing to do is to find the game in template.txt and write the
        data from that file to offset 0x1400 inside the rom on sdcard.

        Keyword Arguments:
        rom -- path to rom file"""

        self.fail_on_non_sky3ds()

        # follow symlink
        rom = os.path.realpath(rom)

        # get rom size and calculate block count
        rom_size = os.path.getsize(rom)
        rom_blocks = int(rom_size / 0x200)

        # get free blocks on sd-card and search for a block big enough for the rom
        start_block = 0
        for free_block in self.free_blocks[::-1]:
            if free_block[1] >= rom_blocks:
                start_block = free_block[0]
                break

        if start_block == 0:
            raise Exception("Not enough free continous blocks")

        self.diskfp.seek(0)
        position_header_length = 0x100

        # find free slot for game (card format is limited to 31 games)
        free_slot = -1
        for i in range(0, int(position_header_length / 0x8) - 1):
            position = struct.unpack("ii", self.diskfp.read(0x8))
            if position == (-1, -1):
                free_slot = i
                break

        if free_slot == -1:
            raise Exception("No free slot found. There can be a maximum of %d games on one card." % int(position_header_length / 0x8))

        # seek to start of rom on sd-card
        self.diskfp.seek(start_block * 0x200)

        # open rom file
        romfp = open(rom, "rb")

        # get card specific data from template.txt
        serial = gamecard.ncsd_serial(romfp)
        sha1 = gamecard.ncch_sha1sum(romfp)

        template_data = titles.get_template(serial, sha1)
        if template_data:
            generated_template = False
            card_data = bytearray.fromhex(template_data['card_data'])

        else:
            generated_template = True
            logging.warning("Automagically creating sky3ds header (this will fail, lol)")
            card_data = bytearray()

            # card crypto + card id + eeprom id(?)
            romfp.seek(0x1244)
            card_data += romfp.read(0x4)
            romfp.seek(0x1240)
            card_data += romfp.read(0x4)
            romfp.seek(0x1248)
            card_data += romfp.read(0x4)

            # CRC16 of NCCH header?
            romfp.seek(0x1000)
            crc16 = titles.crc16(bytearray(romfp.read(0x200)))
            card_data += bytearray(struct.pack("H", crc16)[::-1])
            card_data += bytearray(struct.pack("H", (crc16 << 16 | crc16 ^ 0xffff) & 0xFFFF)[::-1])

            # CTRIMAGE + zero-padding
            card_data += bytearray("CTRIMAGE", "ascii")
            card_data += bytearray([0x00]*0x8)

            # ?!?!?!?
            card_data += bytearray([0x00] * 0x10)

            # zero-padding
            card_data += bytearray([0x00] * 0x10)

            # unique id
            romfp.seek(0x1200)
            card_data += romfp.read(0x40)

            # name
            romfp.seek(0x1150)
            card_data += romfp.read(0x10)
            card_data += bytearray([0x00] * 0xf0)

            # zero-padding
            card_data += bytearray([0x00] * 0x80)

        if use_header_bin:
            header_bin = os.path.join(data_dir,'header.bin')
            if os.path.exists(header_bin):
                logging.info("Injecting headers from header.bin instead of template.txt!")
                try:
                    header_bin_fp = open(header_bin, "rb")
                    rom_header = bytearray(header_bin_fp.read(0x44))
                    header_bin_fp.close()
                    for byte in range(0x40):
                        card_data[0x40+byte] = rom_header[byte]
                except:
                    raise Exception("Error: Can't inject headers from header.bin")

        elif rom[-4:] == ".3dz":
            romfp.seek(0x1200)
            rom_header = romfp.read(0x44)
            if rom_header[0x00:0x10] != bytearray([0xff]*0x10):
                logging.info("Injecting headers from 3dz file instead of template.txt!")
                for byte in range(0x40):
                    card_data[0x40+byte] = rom_header[byte]
                for byte in range(0x4):
                    card_data[0x4+byte] = rom_header[0x40+byte]

        # recalculate checksum for sky3ds header (important after injection from 3dz or header.bin)
        crc16 = titles.crc16(card_data[:-2])
        card_data[-2] = (crc16 & 0xFF00) >> 8
        card_data[-1] = (crc16 & 0x00FF)

        if len(card_data) != 0x200:
            raise Exception("Invalid template data")

        if verbose:
            template  = "\nUsed template:\n"
            template += "** : %s\n" % card_data[0x80:0x90].decode("ascii")
            template += "SHA1: %s\n" % gamecard.ncch_sha1sum(romfp).upper()
            for i in range(0, 0x20):
                line = ""
                for j in range(0, 0x10):
                    line += ("%.2x " % card_data[i*0x10+j]).upper()
                template += line + "\n"
            template += "\n"
            logging.info(template)

        # write rom (with fancy progressbar!)
        romfp.seek(0)
        try:
            if not silent and not progress:
                progress = ProgressBar(widgets=[Percentage(), Bar(), FileTransferSpeed()], maxval=rom_size).start()
        except:
            pass
        written = 0
        while written < rom_size:
            chunk = romfp.read(1024*1024*8)

            self.diskfp.write(chunk)
            os.fsync(self.diskfp)

            written = written + len(chunk)
            try:
                if not silent:
                   progress.update(written)
            except:
                pass
        try:
            if not silent:
                progress.finish()
        except:
            pass

        # seek to slot header and write position + block-count of rom
        self.diskfp.seek(free_slot * 0x8)
        self.diskfp.write(struct.pack("ii", start_block, rom_blocks))

        # add savegame slot
        self.diskfp.seek(0x100000 * (1 + len(self.rom_list)))
        self.diskfp.write(bytearray([0xff]*0x100000))

        self.diskfp.seek(start_block * 0x200 + 0x1400)
        self.diskfp.write(card_data)

        # cleanup
        romfp.close()
        os.fsync(self.diskfp)

        self.update_rom_list()

    def dump_rom(self, slot, output, silent=False, progress=None):
        """Dump rom from sdcard to file

        This opens the rom position header at the specified slot, seeks to
        the start point on sdcard, and just starts dumping data to the output-
        file until the whole rom has been dumped. After dumping sky3ds specific
        data (0x1400 - 0x1600) gets removed from the romfile.

        Keyword Arguments:
        slot -- rom position header slot
        output -- output rom file"""

        self.fail_on_non_sky3ds()

        start = self.rom_list[slot][1]
        rom_size = self.rom_list[slot][2]

        self.diskfp.seek(start)

        outputfp = open(output, "wb")

        # read rom
        try:
            if not silent and not progress:
                progress = ProgressBar(widgets=[Percentage(), Bar(), FileTransferSpeed()], maxval=rom_size).start()
        except:
            pass
        written = 0
        while written < rom_size:
            chunk = self.diskfp.read(1024*1024)

            outputfp.write(chunk)
            os.fsync(outputfp)

            written = written + len(chunk)
            try:
                if not silent:
                    progress.update(written)
            except:
                pass
        try:
            if not silent:
                progress.finish()
        except:
            pass

        # remove sky3ds specific data from
        outputfp.seek(0x1400)
        outputfp.write(bytearray([0xff]*0x200))

        # cleanup
        os.fsync(outputfp)
        outputfp.close()

    # delete rom from sdcard
    def delete_rom(self, slot):
        """Delete rom from sdcard

        This deletes the specified rom from the sdcard. It doesn't actually
        delete any rom data, it justs reorders rom position headers and
        savegames, thereby making the rom space available for new roms.

        Keyword Arguments:
        slot -- rom position header slot"""

        self.fail_on_non_sky3ds()

        current_save = slot

        while current_save < len(self.rom_list):
            self.diskfp.seek(0x100000 * (current_save + 2))
            tmp_savegame = self.diskfp.read(0x100000)
            self.diskfp.seek(0x100000 * (current_save + 1))
            self.diskfp.write(tmp_savegame)
            current_save += 1
        self.diskfp.write(bytearray([0xff]*0x100000))

        # remove slot header and rearrange the rest of the headers
        position_header_length = 0x100
        self.diskfp.seek(0x0)
        raw_positions = list(self.diskfp.read(position_header_length))
        new_raw_positions = bytearray(raw_positions[0:slot*8] + raw_positions[(slot+1)*8:] + [0xff]*8)
        self.diskfp.seek(0x0)
        self.diskfp.write(new_raw_positions)

        self.update_rom_list()

    #####################
    # Savegame Handling #
    #####################

    def dump_savegame(self, slot, output):
        """Dump savegame from sdcard to file

        This code first looks at the actual game header of the rom in the
        specified slot to figure out if this is a Card1 or Card2 savegame based
        game.

        For Card1 savegames it just dumps the savegame from the preallocated
        region of Card1 savegames (0x100000 - 0x2000000, 31 each 0x100000 / 1MB).
        The savegame file has 'CTR_SAVE', the product code of the game, a mark
        that this is a Card1 savegame and some padding in front of the actual
        savegame data.

        For Card2 savegames it reads the writable_address from the games
        ncsd-header, and dumps 10MB from that location to a file.
        The savegame file also has 'CTR_SAVE', the product-code and a (different)
        mark in front of the actual savegame as well as the type and size of
        (emulated) game chip.

        Keyword Arguments:
        slot -- rom slot
        output -- output savegame file"""

        self.fail_on_non_sky3ds()

        if slot >= len(self.rom_list):
            raise Exception("Slot not found")

        self.diskfp.seek(0)

        self.diskfp.seek(self.rom_list[slot][1])
        ncsd_header = gamecard.ncsd_header(self.diskfp.read(0x1200))

        savegamefp = open(output, "wb")

        # 0x00 CTR_SAVE
        savegamefp.write(b'CTR_SAVE')

        # 0x08 Product Code
        savegamefp.write(bytearray(ncsd_header['product_code'].encode('ascii')))

        # Zero-Padding + Save Type (0x00 = Card1, 0x01 = Card2)
        if ncsd_header['card_type'] == 'Card1':
            savegamefp.write(bytearray([0x00, 0x00]))
        else:
            savegamefp.write(bytearray([0x00, 0x01]))

        # Nand save offset / Writable Address
        self.diskfp.seek(self.rom_list[slot][1] + 0x200)
        savegamefp.write(self.diskfp.read(0x4))

        # Unique ID (0x40 bytes but only 0x10 really used)
        self.diskfp.seek(self.rom_list[slot][1] + 0x1440)
        savegamefp.write(self.diskfp.read(0x40))

        # Savegame Data
        if ncsd_header['card_type'] == 'Card1':
            # from card1 region (byte 1M - 32M on disk)
            self.diskfp.seek(0x100000 * (slot + 1))
            savegamefp.write(self.diskfp.read(0x100000))
        else:
            # from writable region in rom
            self.diskfp.seek(self.rom_list[slot][1] + ncsd_header['writable_address'])
            for i in range(0, 10):
                savegamefp.write(self.diskfp.read(0x100000))

        savegamefp.close()

    def find_game(self, product_code):
        """Find a game on sdcard by product-code

        This function is used to automatically restore savegames to the right game.
        It basically gets the product-codes of all roms on sdcard and compares it
        to the given argument.

        Keyword Arguments:
        product_code -- product-code to look for on sdcard"""

        self.fail_on_non_sky3ds()

        slot = -1
        rom_count = 0
        for rom in self.rom_list:
            self.diskfp.seek(rom[1])
            ncsd_header = gamecard.ncsd_header(self.diskfp.read(0x1200))
            if ncsd_header['product_code'] == product_code:
                return (rom_count, ncsd_header)
            rom_count+=1
        return (None, None)

    def write_savegame(self, savefile):
        """Restore savegame from file to sdcard

        This function (re)stores a given savegame file to the corresponding
        location on sdcard.
        Since the savegame backup has the product-code inside this function
        doesn't need any further arguments.

        It first opens the savegame backup, validates the header and retrieves
        the product-code to use find_game(product_code) for looking up the slot
        of the corresponding game.
        The savegame file itself stores the information wether it's Card1 or
        Card2, but i found it easier to just read the ncsd-header of the game.

        For Card1 savegames the file gets written to the corresponding slot
        in the region of Card1-savegames.

        For Card2 savegames it gets written to the writable_address offset of
        the game."""

        self.fail_on_non_sky3ds()

        savegamefp = open(savefile, "rb")

        # CTR_SAVE
        ctr_save = savegamefp.read(0x8)
        if ctr_save != b'CTR_SAVE':
            raise Exception("Not a valid savegame")

        # Product Code
        product_code = savegamefp.read(0xa).decode('ascii')
        slot,ncsd_header = self.find_game(product_code)
        if slot == None:
            raise Exception("Game not on disk")

        # Zero Padding (ignored)
        savegamefp.read(0x1)

        # Save Type (ignored, read directly from ncsd_header)
        savegamefp.read(0x1)

        # NAND save offset (ignored, read directly from ncsd_header)
        savegamefp.read(0x4)

        # Unique ID (+ recalculate crc)
        self.diskfp.seek(self.rom_list[slot][1] + 0x1440)
        self.diskfp.write(savegamefp.read(0x40))

        self.diskfp.seek(self.rom_list[slot][1] + 0x1400)
        card_data = self.diskfp.read(0x200)
        crc16 = titles.crc16(bytearray(card_data[:-2]))
        self.diskfp.seek(self.rom_list[slot][1] + 0x1400 + 0x200 - 0x2)
        self.diskfp.write(bytearray([(crc16 & 0xFF00) >> 8, crc16 & 0x00FF]))

        # Savegame data
        if ncsd_header['card_type'] == 'Card1':
            self.diskfp.seek(0x100000 * (slot + 1))
            self.diskfp.write(savegamefp.read(0x100000))
            os.fsync(self.diskfp)
        elif ncsd_header['card_type'] == 'Card2':
            self.diskfp.seek(self.rom_list[slot][1] + ncsd_header['writable_address'])
            for i in range(0, 10):
                self.diskfp.write(savegamefp.read(0x100000))
                os.fsync(self.diskfp)

        savegamefp.close()


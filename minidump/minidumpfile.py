#!/usr/bin/env python3
#
# Author:
#  Tamas Jos (@skelsec)
#

import io
import sys
import enum
import struct
import logging

from minidump.header import MinidumpHeader
from minidump.minidumpreader import MinidumpFileReader
from minidump.streams import *
from minidump.common_structs import *
from minidump.constants import MINIDUMP_STREAM_TYPE, OFFSETS, POINTER_SIZE
from minidump.directory import MINIDUMP_DIRECTORY
from minidump.streams.SystemInfoStream import PROCESSOR_ARCHITECTURE


class MinidumpFile:
	def __init__(self):
		self.filename = None
		self.file_handle = None
		self.header = None
		self.directories = []

		self.threads_ex = None
		self.threads = None
		self.modules = None
		self.memory_segments = None
		self.memory_segments_64 = None
		self.sysinfo = None
		self.comment_a = None
		self.comment_w = None
		self.exception = None
		self.handles = None
		self.unloaded_modules = None
		self.misc_info = None
		self.memory_info = None
		self.thread_info = None

	@staticmethod
	def parse(filename):
		mf = MinidumpFile()
		mf.filename = filename
		mf.file_handle = open(filename, 'rb')
		mf._parse()
		return mf

	@staticmethod
	def parse_external(file_handle, filename = ''):
		"""
		External file handle must be an object that exposes basic file IO functionality
		that you'd get by python's file buffer (read, seek, tell etc.)
		"""
		mf = MinidumpFile()
		mf.filename = filename
		mf.file_handle = file_handle
		mf._parse()
		return mf

	@staticmethod
	def parse_bytes(data):
		return MinidumpFile.parse_buff(io.BytesIO(data))

	@staticmethod
	def parse_buff(buffer):
		mf = MinidumpFile()
		mf.file_handle = buffer
		mf._parse()
		return mf

	def get_reader(self):
		return MinidumpFileReader(self)

	def _parse(self):
		self.__parse_header()
		self.__parse_directories()
		self.__parse_peb()

	def __read_unicode_string_property(self, buff_reader, addr, x64):
		buff_reader.move(addr)
		string_length = int.from_bytes(buff_reader.read(2), "little")
		if not string_length:
			return ""
		buff_reader.move(addr + OFFSETS[x64]["buffer"])
		buff_va = int.from_bytes(buff_reader.read(POINTER_SIZE[x64]), "little")
		buff_reader.move(buff_va)
		return buff_reader.read(string_length).decode("utf-16")

	def __parse_peb(self):
		self.x64 = (self.memory_segments_64 is not None) or (self.memory_segments and  any(mem.start_virtual_address > 0xFFFFFFFF for mem in self.memory_segments))
		offset_index = self.x64

		reader = self.get_reader()
		buff_reader = reader.get_buffered_reader()

		buff_reader.move(self.threads.threads[0].Teb + OFFSETS[offset_index]["peb"])

		self.peb_address = int.from_bytes(buff_reader.read(POINTER_SIZE[self.x64]), "little")

		buff_reader.move(self.peb_address + OFFSETS[offset_index]["being_debugged"])
		self.being_debugged = int.from_bytes(buff_reader.read(1), "little")

		buff_reader.move(self.peb_address + OFFSETS[offset_index]["image_base_address"])
		self.image_base_address = int.from_bytes(buff_reader.read(POINTER_SIZE[self.x64]), "little")

		buff_reader.move(self.peb_address + OFFSETS[offset_index]["process_parameters"])
		process_parameters = int.from_bytes(buff_reader.read(POINTER_SIZE[self.x64]), "little")

		self.image_path = self.__read_unicode_string_property(
			buff_reader, process_parameters + OFFSETS[offset_index]["image_path"], self.x64
		)

		self.command_line = self.__read_unicode_string_property(
			buff_reader, process_parameters + OFFSETS[offset_index]["command_line"], self.x64
		)

		self.window_title = self.__read_unicode_string_property(
			buff_reader, process_parameters + OFFSETS[offset_index]["window_title"], self.x64
		)

		self.dll_path = self.__read_unicode_string_property(buff_reader, process_parameters + OFFSETS[offset_index]["dll_path"],
												self.x64)

		self.current_directory = self.__read_unicode_string_property(
			buff_reader, process_parameters + OFFSETS[offset_index]["current_directory"], self.x64
		)

		buff_reader.move(process_parameters + OFFSETS[offset_index]["standard_input"])
		self.standard_input = int.from_bytes(buff_reader.read(POINTER_SIZE[self.x64]), "little")

		buff_reader.move(process_parameters + OFFSETS[offset_index]["standard_output"])
		self.standard_output = int.from_bytes(buff_reader.read(POINTER_SIZE[self.x64]), "little")

		buff_reader.move(process_parameters + OFFSETS[offset_index]["standard_error"])
		self.standard_error = int.from_bytes(buff_reader.read(POINTER_SIZE[self.x64]), "little")

		# Parse Environment Variables from PEB
		self.environment_variables = []
		buff_reader.move(process_parameters + OFFSETS[offset_index]["environment_variables"])
		environment_va = int.from_bytes(buff_reader.read(POINTER_SIZE[self.x64]), "little")
		buff_reader.move(environment_va)

		env_buffer = buff_reader.read(buff_reader.current_segment.end_address - buff_reader.current_position)
		while (env_len := env_buffer.find(b"\x00\x00")) and (env_len != -1):
			decoded_env = (env_buffer[:env_len] + b"\x00").decode("utf-16")
			name = decoded_env.split("=")[0]
			value = decoded_env.split("=")[1]
			self.environment_variables.append({"name": name, "value": value})
			environment_va += (len(decoded_env) + 1) * 2
			buff_reader.move(environment_va)
			env_buffer = buff_reader.read(buff_reader.current_segment.end_address - buff_reader.current_position)


	def __parse_header(self):
		self.header = MinidumpHeader.parse(self.file_handle)
		for i in range(0, self.header.NumberOfStreams):
			self.file_handle.seek(self.header.StreamDirectoryRva + i * 12, 0 )
			minidump_dir = MINIDUMP_DIRECTORY.parse(self.file_handle)

			if minidump_dir:
				self.directories.append(minidump_dir)
			else:
				self.file_handle.seek(self.header.StreamDirectoryRva + i * 12, 0 )
				user_stream_type_value = MINIDUMP_DIRECTORY.get_stream_type_value(self.file_handle)
				logging.debug('Found Unknown UserStream directory Type: %x' % (user_stream_type_value))

	def __parse_directories(self):

		for dir in self.directories:
			if dir.StreamType == MINIDUMP_STREAM_TYPE.UnusedStream:
				logging.debug('Found UnusedStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				continue # Reserved. Do not use this enumeration value.
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.ReservedStream0:
				logging.debug('Found ReservedStream0 @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				continue # Reserved. Do not use this enumeration value.
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.ReservedStream1:
				logging.debug('Found ReservedStream1 @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				continue # Reserved. Do not use this enumeration value.
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.ThreadListStream:
				logging.debug('Found ThreadListStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.threads = MinidumpThreadList.parse(dir, self.file_handle)
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.ModuleListStream:
				logging.debug('Found ModuleListStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.modules = MinidumpModuleList.parse(dir, self.file_handle)
				#logging.debug(str(modules_list))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.MemoryListStream:
				logging.debug('Found MemoryListStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.memory_segments = MinidumpMemoryList.parse(dir, self.file_handle)
				#logging.debug(str(self.memory_segments))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.SystemInfoStream:
				logging.debug('Found SystemInfoStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.sysinfo = MinidumpSystemInfo.parse(dir, self.file_handle)
				#logging.debug(str(self.sysinfo))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.ThreadExListStream:
				logging.debug('Found ThreadExListStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.threads_ex = MinidumpThreadExList.parse(dir, self.file_handle)
				#logging.debug(str(self.threads_ex))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.Memory64ListStream:
				logging.debug('Found Memory64ListStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.memory_segments_64 = MinidumpMemory64List.parse(dir, self.file_handle)
				#logging.debug(str(self.memory_segments_64))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.CommentStreamA:
				logging.debug('Found CommentStreamA @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.comment_a = CommentStreamA.parse(dir, self.file_handle)
				#logging.debug(str(self.comment_a))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.CommentStreamW:
				logging.debug('Found CommentStreamW @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.comment_w = CommentStreamW.parse(dir, self.file_handle)
				#logging.debug(str(self.comment_w))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.ExceptionStream:
				logging.debug('Found ExceptionStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.exception = ExceptionList.parse(dir, self.file_handle)
				#logging.debug(str(self.comment_w))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.HandleDataStream:
				logging.debug('Found HandleDataStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.handles = MinidumpHandleDataStream.parse(dir, self.file_handle)
				#logging.debug(str(self.handles))
				continue

			elif dir.StreamType == MINIDUMP_STREAM_TYPE.FunctionTableStream:
				logging.debug('Found FunctionTableStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				logging.debug('Parsing of this stream type is not yet implemented!')
				continue

			elif dir.StreamType == MINIDUMP_STREAM_TYPE.UnloadedModuleListStream:
				logging.debug('Found UnloadedModuleListStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.unloaded_modules = MinidumpUnloadedModuleList.parse(dir, self.file_handle)
				#logging.debug(str(self.unloaded_modules))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.MiscInfoStream:
				logging.debug('Found MiscInfoStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.misc_info = MinidumpMiscInfo.parse(dir, self.file_handle)
				#logging.debug(str(self.misc_info))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.MemoryInfoListStream:
				logging.debug('Found MemoryInfoListStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.memory_info = MinidumpMemoryInfoList.parse(dir, self.file_handle)
				#logging.debug(str(self.memory_info))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.ThreadInfoListStream:
				logging.debug('Found ThreadInfoListStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				self.thread_info = MinidumpThreadInfoList.parse(dir, self.file_handle)
				logging.debug(str(self.thread_info))
				continue
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.SystemMemoryInfoStream:
				logging.debug('Found SystemMemoryInfoStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				logging.debug('SystemMemoryInfoStream parsing is not implemented (Missing documentation)')
				continue

			elif dir.StreamType == MINIDUMP_STREAM_TYPE.JavaScriptDataStream:
				logging.debug('Found JavaScriptDataStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				logging.debug('JavaScriptDataStream parsing is not implemented (Missing documentation)')

			elif dir.StreamType == MINIDUMP_STREAM_TYPE.ProcessVmCountersStream:
				logging.debug('Found ProcessVmCountersStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				logging.debug('ProcessVmCountersStream parsing is not implemented (Missing documentation)')

			elif dir.StreamType == MINIDUMP_STREAM_TYPE.TokenStream:
				logging.debug('Found TokenStream @%x Size: %d' % (dir.Location.Rva, dir.Location.DataSize))
				logging.debug('TokenStream parsing is not implemented (Missing documentation)')

			else:
				logging.debug('Found Unknown Stream! Type: %s @%x Size: %d' % (dir.StreamType.name, dir.Location.Rva, dir.Location.DataSize))

			"""
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.HandleOperationListStream:
			elif dir.StreamType == MINIDUMP_STREAM_TYPE.LastReservedStream:

			"""
		try:
			self.__parse_thread_context()
		except Exception as e:
			logging.exception('Thread context parsing error!')

	def __parse_thread_context(self):
		if not self.sysinfo or not self.threads:
			return
		for thread in self.threads.threads:
			rva = thread.ThreadContext.Rva
			self.file_handle.seek(rva)
			if self.sysinfo.ProcessorArchitecture == PROCESSOR_ARCHITECTURE.AMD64:
				thread.ContextObject = CONTEXT.parse(self.file_handle)
			elif self.sysinfo.ProcessorArchitecture == PROCESSOR_ARCHITECTURE.INTEL:
				thread.ContextObject = WOW64_CONTEXT.parse(self.file_handle)


	def __str__(self):
		t = '== Minidump File ==\n'
		t += str(self.header)
		t += str(self.sysinfo)
		for dir in self.directories:
			t += str(dir) + '\n'
		for mod in self.modules.modules:
			t += str(mod) + '\n'
		if self.memory_segments is not None:
			for segment in self.memory_segments.memory_segments:
				t+= str(segment) + '\n'

		if self.memory_segments_64 is not None:
			for segment in self.memory_segments_64.memory_segments:
				t+= str(segment) + '\n'

		return t

"""A wrapper to use libusb. Default on linux, on Windows you have to install a generic driver for your camera"""

import sys
import usb.core, usb.util

from . import *
from ...util import *

PtpHeader = Struct('PtpHeader', [
 ('size', Struct.INT32),
 ('type', Struct.INT16),
 ('code', Struct.INT16),
 ('transaction', Struct.INT32),
])

MscCommandBlockWrapper = Struct('MscCommandBlockWrapper', [
 ('signature', Struct.STR % 4),
 ('tag', Struct.INT32),
 ('dataTransferLength', Struct.INT32),
 ('flags', Struct.INT8),
 ('lun', Struct.INT8),
 ('commandLength', Struct.INT8),
 ('command', Struct.STR % 16),
])

MscCommandStatusWrapper = Struct('MscCommandStatusWrapper', [
 ('signature', Struct.STR % 4),
 ('tag', Struct.INT32),
 ('dataResidue', Struct.INT32),
 ('status', Struct.INT8),
])

class Context(object):
 """Use this in a with statement when using the driver"""
 def __enter__(self):
  return sys.modules[__name__]

 def __exit__(self, type, value, traceback):
  pass


def listDevices(vendor):
 """Lists all detected USB devices"""
 for dev in usb.core.find(find_all=True, idVendor=vendor):
  interface = next((interface for config in dev for interface in config), None)
  if interface:
   yield UsbDevice(dev, dev.idVendor, dev.idProduct, interface.bInterfaceClass)


class UsbDriver(object):
 """Bulk reading and writing to USB devices"""
 USB_ENDPOINT_TYPE_BULK = 2
 USB_ENDPOINT_MASK = 1
 USB_ENDPOINT_OUT = 0
 USB_ENDPOINT_IN = 1

 def __init__(self, device):
  self.dev = device.handle
  self.epIn = self._findEndpoint(self.USB_ENDPOINT_TYPE_BULK, self.USB_ENDPOINT_IN)
  self.epOut = self._findEndpoint(self.USB_ENDPOINT_TYPE_BULK, self.USB_ENDPOINT_OUT)

 def __del__(self):
  usb.util.dispose_resources(self.dev)

 def _findEndpoint(self, type, direction):
  interface = self.dev.get_active_configuration()[(0, 0)]
  for ep in interface:
   if ep.bmAttributes == type and ep.bEndpointAddress & self.USB_ENDPOINT_MASK == direction:
    return ep.bEndpointAddress
  raise Exception('No endpoint found')

 def reset(self):
  try:
   if self.dev.is_kernel_driver_active(0):
    self.dev.detach_kernel_driver(0)
  except NotImplementedError:
   pass
  if self.dev._ctx.backend.__module__ == 'usb.backend.libusb1':
   self.dev.reset()

 def read(self, length):
  return self.dev.read(self.epIn, length).tostring()

 def write(self, data):
  return self.dev.write(self.epOut, data)


class MscDriver(UsbDriver):
 """Communicate with a USB mass storage device"""
 MSC_OC_REQUEST_SENSE = 0x03

 DIRECTION_WRITE = 0
 DIRECTION_READ = 0x80

 def _writeCommand(self, direction, command, dataSize, tag=0, lun=0):
  self.write(MscCommandBlockWrapper.pack(
   signature = b'USBC',
   tag = tag,
   dataTransferLength = dataSize,
   flags = direction,
   lun = lun,
   commandLength = len(command),
   command = command.ljust(16, b'\0'),
  ))

 def _readResponse(self, failOnError=False):
  response = MscCommandStatusWrapper.unpack(self.read(MscCommandStatusWrapper.size))
  if response.signature != b'USBS':
   raise Exception('Wrong status signature')
  if response.status != 0:
   if failOnError:
    raise Exception('Mass storage error')
   else:
    return self.requestSense()
  return MSC_SENSE_OK

 def requestSense(self):
  size = 18
  response, data = self.sendReadCommand(dump8(self.MSC_OC_REQUEST_SENSE) + 3*b'\0' + dump8(size) + b'\0', size, failOnError=True)
  return parseMscSense(data)

 def sendCommand(self, command, failOnError=False):
  self._writeCommand(self.DIRECTION_WRITE, command, 0)
  return self._readResponse(failOnError)

 def sendWriteCommand(self, command, data, failOnError=False):
  self._writeCommand(self.DIRECTION_WRITE, command, len(data))

  stalled = False
  try:
   self.write(data)
  except usb.core.USBError:
   # Write stall
   stalled = True
   self.dev.clear_halt(self.epOut)

  sense = self._readResponse(failOnError)
  if stalled and sense == MSC_SENSE_OK:
   raise Exception('Mass storage write error')
  return sense

 def sendReadCommand(self, command, size, failOnError=False):
  self._writeCommand(self.DIRECTION_READ, command, size)

  stalled = False
  data = None
  try:
   data = self.read(size)
  except usb.core.USBError:
   # Read stall
   stalled = True
   self.dev.clear_halt(self.epIn)

  sense = self._readResponse(failOnError)
  if stalled and sense == MSC_SENSE_OK:
   raise Exception('Mass storage read error')
  return sense, data


class MtpDriver(UsbDriver):
 """Send and receive PTP/MTP packages to a device. Inspired by libptp2"""
 MAX_PKG_LEN = 512
 TYPE_COMMAND = 1
 TYPE_DATA = 2
 TYPE_RESPONSE = 3

 def _writePtp(self, type, code, transaction, data=b''):
  self.write(PtpHeader.pack(
   size = PtpHeader.size + len(data),
   type = type,
   code = code,
   transaction = transaction,
  ) + data)

 def _readPtp(self):
  data = b''
  while data == b'':
   data = self.read(self.MAX_PKG_LEN)
  header = PtpHeader.unpack(data)
  if header.size > self.MAX_PKG_LEN:
   data += self.read(header.size - self.MAX_PKG_LEN)
  return header.type, header.code, header.transaction, data[PtpHeader.size:PtpHeader.size+header.size]

 def _readData(self):
  type, code, transaction, data = self._readPtp()
  if type != self.TYPE_DATA:
   raise Exception('Wrong response type: 0x%x' % type)
  return data

 def _readResponse(self):
  type, code, transaction, data = self._readPtp()
  if type != self.TYPE_RESPONSE:
   raise Exception('Wrong response type: 0x%x' % type)
  return code

 def _writeInitialCommand(self, code, args):
  try:
   self.transaction += 1
  except AttributeError:
   self.transaction = 0
  self._writePtp(self.TYPE_COMMAND, code, self.transaction, b''.join([dump32le(arg) for arg in args]))

 def sendCommand(self, code, args):
  """Send a PTP/MTP command without data phase"""
  self._writeInitialCommand(code, args)
  return self._readResponse()

 def sendWriteCommand(self, code, args, data):
  """Send a PTP/MTP command with write data phase"""
  self._writeInitialCommand(code, args)
  self._writePtp(self.TYPE_DATA, code, self.transaction, data)
  return self._readResponse()

 def sendReadCommand(self, code, args):
  """Send a PTP/MTP command with read data phase"""
  self._writeInitialCommand(code, args)
  data = self._readData()
  return self._readResponse(), data

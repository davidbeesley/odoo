# Part of Odoo. See LICENSE file for full copyright and licensing details.

from serial.tools.list_ports import comports

from odoo.addons.iot_drivers.tools.system import IS_WINDOWS, IS_X86
from odoo.addons.iot_drivers.interface import Interface


class SerialInterface(Interface):
    connection_type = 'serial'
    allow_unsupported = True

    def get_devices(self):
        serial_devices = {
            port.device: {'identifier': port.device}
            for port in comports()
            if IS_WINDOWS or port.device != '/dev/ttyAMA10'
            # RPI 5 uses ttyAMA10 as a console serial port for system messages: odoo interprets it as scale -> avoid it
            and not (IS_X86 and port.device.startswith('/dev/ttyS'))
            # x86 VMs have unconfigured virtual serial ports (/dev/ttyS*) that cause I/O errors - skip them
        }
        return serial_devices

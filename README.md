RoomCtl
=======
Originally a private project, something quick & least-resistance. 
I guess I do not mind anybody making use of the material, so welcome.

Peripherals used:
- Custom CondCtl device, now relegated mostly to reading temperature sensors and controlling fan relays and heater, making use of embedded temperature control logic.
- Arduino board for additional peripherals (servos for blinds, Air Conditioner through IR transmitter, reading pressure sensor)

For both AT modem-like command set through serial command interface is used, see the sources for details.
RoomCtl.ino for Arduino sketch, condsrv.py for overall high-level control and web interface.

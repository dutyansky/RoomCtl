#!/usr/bin/python
#
# CondSrv.py -- Web inteface and high-level controls for miscellaneous apartment
#  housekeping.
#
# See RoomCtl.ino for Arduino sketch.
#
# Assumes Python 2.7.3, originally run on OpenWRT router box
#
import time
import serial
import re
import Image,ImageDraw,ImageFont
import cStringIO
import datetime
import threading
import sys
import glob
import shelve
import ConfigParser
import resource
import ftplib
import urllib2


sys.stderr = sys.stdout

import os, errno

import pdb

from cgi import parse_qs, escape
from wsgiref.simple_server import (make_server, WSGIRequestHandler)

from paste.auth.digest import digest_password, AuthDigestHandler
from functools import wraps

#
# Global constants
#
HistLen = 60*24/2       # Length for all history arrays, samples per 24hrs
#
# Global configuration parameters
#
RoomTstring = ''
ExtTstring = ''
AuxTstring = ''
UserName = ''
UserPass = ''
#
CfgAcCtlEnabled = False	# AC control enabled
CfgFanCoolingEnabled = False # "Use fan for cooling"  enabled
CfgFanOnTime = 0        # Fan on forced time, 0=Disabled
CfgFanOffTime = 0       # Fan off forced time
CfgEvents = []          # Scheduler events
#
# History arrays and global state variables
#
ExtT = []               # List of external temperatures
PrevExtT = []           # List of previous days external temperatures
RoomT = []              # List of room temperatures for current day
RoomTavr = []
BaroP = []              # List of baro pressures for current day
AuxT = []               # List of fridge temperatures for current day
ClimateHist = []        # List of climate on/off states
FanHist = []            # List of fan on/off forced states
TgT = []
AcT = []
LastExtT, LastRoomT, LastRoomTavr, LastAuxT = 0,0,0,0 # Recent temperature measurements
LastPressure = 0
ClimateOn = False       # "Climate control on" state
FanOn = False           # Current low-level "forced fan" flag
TargetTemp = 0          # Target room temperature
MinutesToFanOn = 0
Img = []

LogHandle = 0

RecentLogLines = []
GismeteoT = []		# [n,3] GisMeteo hourly forecast
Sun = 0			# [sunrise, sunset] time in minutes

#
# LogLine() - Log line to file & console, prepending datetime stamp
#
def LogLine(s):
 s1 = "["+DateTime()+"] "+s
 LogHandle.write(s1+'\n')
 LogHandle.flush()
 print(s1)
 for i in range(len(RecentLogLines)-1):
   RecentLogLines[i] = RecentLogLines[i+1]
 RecentLogLines[len(RecentLogLines)-1] = s1

# Format: "31/Dec/2012 15:31:16"
def DateTime():
 d = datetime.datetime.now()
 return d.strftime("%d/%b/%Y %H:%M:%S");

#
# com = OpenPort('/dev/ttyUSB0', rate);
#
def OpenPort(devName, rate):
 com = serial.Serial(port=devName, baudrate=rate,
         parity=serial.PARITY_NONE,
         stopbits=serial.STOPBITS_ONE,
         bytesize=serial.EIGHTBITS,
         xonxoff=0,
         rtscts=0,
         timeout=2);
 com.open();
 com.isOpen();
 return com;

# Exception class to return
class PortError(Exception):
 def __init__(self, name):
  self.name = name
 def __str__(self):
  return repr(self.name)

#
# Reconnect(PortError x) - try to reconnect to the port, probably at different index
#
def Reconnect(x):
 global comC, comR

 for i in range(3):
   time.sleep(2)
   if re.search(".*ttyUSB.*", x.name):
     (comC, nameX) = FindPort("/dev/ttyUSB*", 38400)
     com = comC
   else:
     (comR, nameX) = FindPort("/dev/ttyACM*", 9600)
     com = comR
   time.sleep(2);
   if com != None:  # Successful reconnect
     LogLine("Error encountered on \""+x.name+"\", reconnected as \""+nameX+"\"");
     return
 sys.exit("*** Could not reconnect \""+x.name+"\"")


#
# AcController - Air conditioning unit control
#
class AcController(object):
  AcCalibrationTimeout = 30               # Time till we start calibration measurements
  AcMinutesToCheck = AcCalibrationTimeout # Reset activation timeout
  AcCalibration = 1                       # Reset calibration offset to default +1
  AcCurrentTemp = -1                      # Reset current cached T to impossible

  def __init__(self):
    self.AcLock = threading.RLock()

  def synchronous(tlockname):
    """A decorator to place an instance based lock around a method """
    def _synched(func):
        @wraps(func)
        def _synchronizer(self, *args, **kwargs):
            tlock = self.__getattribute__(tlockname)
            tlock.acquire()
            try:
                return func(self, *args, **kwargs)
            finally:
                tlock.release()
        return _synchronizer
    return _synched


  # Calibrate(temperature) - Return temperature with current AC temp calibration factor
  def Calibrate(self, t):
    a = t
    if t != 0:
      a = a + self.AcCalibration
    return int(a)

  # SetTemperature() - Set target temperature for AC
  @synchronous('AcLock')
  def SetTemperature(self, t):
    self.AcMinutesToCheck = self.AcCalibrationTimeout
    if ClimateOn and CfgAcCtlEnabled:
      newT = self.Calibrate(t)
      if newT != self.AcCurrentTemp:
        self.AcCurrentTemp = newT
        LogLine("AC set to %d"%newT)
        WaitReplySafe(comR, 'ATAC=%d'%newT)
      else:
        LogLine("AC already set to %d"%newT)

  @synchronous('AcLock')
  def ResetCalibration(self, newAcSet):
    if self.AcCalibration != newAcSet - TargetTemp:
      self.AcCalibration = newAcSet - TargetTemp
      LogLine("AC calibration reset, new: %d, new AC target: %d"%(self.AcCalibration, self.Calibrate(TargetTemp)))
      self.SetTemperature(TargetTemp)

  @synchronous('AcLock')
  def AdjustCalibration(self, at, at1, TargetTemp):
    if self.AcMinutesToCheck > 0 and CfgAcCtlEnabled and ClimateOn:
      self.AcMinutesToCheck -= 2
      if self.AcMinutesToCheck <= 0:
        LogLine("AC calibration control activated")

    if self.AcMinutesToCheck <= 0 and CfgAcCtlEnabled and ClimateOn:
      if at <= TargetTemp - 1 and at <= at1:
        if self.AcCalibration < 5:
          self.AcCalibration += 1
          LogLine("AC calibration increase, new: %d, new AC target: %d"%(self.AcCalibration, self.Calibrate(TargetTemp)))
          self.SetTemperature(TargetTemp)
        else:
          LogLine("AC calibration already at %d, cannot increase"%self.AcCalibration)
      elif at >= TargetTemp + 1 and at >= at1:
        if self.AcCalibration > -5:
          self.AcCalibration -= 1
          LogLine("AC calibration decrease, new: %d, new AC target: %d"%(self.AcCalibration, self.Calibrate(TargetTemp)))
          self.SetTemperature(TargetTemp)
        else:
          LogLine("AC calibration already at %d, cannot decrease"%self.AcCalibration)


# AC controller object
Ac = AcController()

#
# [] = WaitReplySafe(com [, "Command"])
#
def WaitReplySafe(com, cmd=""):
 """Wait for [optional command] reply, with several attempts in case of comms errors. See WaitReply()"""

 for i in range(3):
   try:
     l = WaitReply(com, cmd)
   except PortError, x:
     Reconnect(x)
     if re.search(".*ttyUSB.*", x.name):
       com = comC
     else:
       com = comR
   else:
     return l;  # Exit here for success
 LogLine("*** WaitReplySafe retry limit exceeded on cmd=\"%s\""%cmd)
 raise PortError(com.port)
 return l

#
# [] = WaitReply(com [, "Command"])
#
def WaitReply(com, cmd=""):
 global LockC, LockR

 try:
  if com == comC:
   LockC.acquire();
  else:
   if com == comR:
    LockR.acquire();

  r = [];				# Clear list of returned lines
  if cmd:				# Command specified
   com.write(cmd+'\r');

  tmoCnt = 5;
  for i in range(0, 200):
   s = com.readline();
   s = s.strip();
   if s == "":
     tmoCnt = tmoCnt-1
     if tmoCnt == 0:
       raise PortError(com.port)

#   sys.stdout.write("Re:\""+s+"\"\r\n");

   if s and not re.search('AT.*', s):	# Not "AT..." echo returned
    if s == "OK":			# "OK" (end of output)
     return r;				# so return accumulated array
    else:
     r.append(s);

  sys.exit("Too many lines received in reply to command \""+cmd+"\"");

 except OSError, ex:
  if ex.errno != 5:
   sys.exit("Unexpected (not errno 5) OSError in WaitReply:\""+ex.strerror+"\"")
  LogLine("OSError:5 encountered on \""+com.port+"\"")
  raise PortError(com.port)

 finally:
  if com == comC:
   LockC.release();
  else:
   if com == comR:
    LockR.release();

#
# FanCtl() - Control fan and update local state
#
def FanCtl(com, s):
  global FanOn
  WaitReplySafe(com, s)
  FanOn = s == 'AT*ENH=2'       # Set to True if forced on

#
# MasterOff() - Disable all climate control functions
# Inputs:
#
#       Ignored
#
def MasterOff(com, s):
  global comC, comR, CfgAcCtlEnabled, ClimateOn
  if ClimateOn:
    WaitReplySafe(comC, 'AT*MOD=0')       # Switch CondCtl off
    Ac.SetTemperature(0)                  # Switch AC off
    ClimateOn = False

#
# MasterOn() - Enable all climate control functions
# Inputs:
#
#       Ignored
#
def MasterOn(com, s):
  global comC, comR, CfgTemp, CfgAcCtlEnabled, TargetTemp, ClimateOn
  if not ClimateOn:
    WaitReplySafe(comC, 'AT*MOD=3')     # Switch CondCtl on
    ClimateOn = True                    # Note: Needed here!, for SetTemperature below to work
    Ac.SetTemperature(TargetTemp)

#
# Helper functions for getting various peripheral states
#
def GetCurrentMode():
 global comC
 t=re.search('.*\]=(.+)', WaitReplySafe(comC, 'AT*mod$')[0]);
 if t:
  r = 'On' if t.group(1) == '3' else 'Off'
 else:
  r = 'N/A'
# print 'Current Mode:'+r;
 return r;

def GetCurrentHighFanMode():
 global comC
 t=re.search('.*\]=(.+)', WaitReplySafe(comC, 'AT*ENH$')[0]);
 if t:
  r = 'Forced' if t.group(1) == '2' else 'On' if t.group(1) == '1' else 'Off'
 else:
  r = 'N/A'
# print 'Current HF Mode:'+r;
 return r;

def GetCurrentLowFanMode():
 global comC
 t=re.search('.*\]=(.+)', WaitReplySafe(comC, 'AT*ENL$')[0]);
 if t:
  r = 'Forced' if t.group(1) == '2' else 'On' if t.group(1) == '1' else 'Off'
 else:
  r = 'N/A'
# print 'Current LF Mode:'+r;
 return r;

def GetCurrentBlindsMode():
 global comR
 s = WaitReplySafe(comR, 'ATSRVR')[0]
 t=re.search('.*:(.+)', s);
 if t:
  r = t.group(1);
 else:
  r = 'N/A'
# print 'Current Blinds Mode:'+r;
 return r;


# Return current temperatures
# xt,rt,ft = GetTemperatures()
def GetTemperatures():
 global comC, LastExtT, LastRoomT, LastAuxT
 extT = 255
 roomT = 255
 auxT = 255

 cnt = 0;
 while extT >= 255 or roomT >= 255 or auxT >= 255:
  cnt += 1
  if cnt >= 100:
    LogLine("*** ATTS retry limit exceeded in GetTemperatures()")

  l = WaitReplySafe(comC, "ATTS")
  for ll in l:
    m = re.search(RoomTstring, ll)   # Room temperature
    if m:
      t = float(m.group(1))

      if t >= 16384:                # Correction for negative numbers representation in diag output
        t = t - 32768

      if t >= 255:
        LogLine("*** Invalid value read in ATTS for RoomT: %4.1f"%t)
      else:
        roomT = t

    m = re.search(AuxTstring, ll)   # Aux temperature
    if m:
      t = float(m.group(1))

      if t >= 16384:                # Correction for negative numbers representation in diag output
        t = t - 32768

      if t >= 255:
        LogLine("*** Invalid value read in ATTS for AuxT: %4.1f"%t)
      else:
        auxT = t

    m = re.search(ExtTstring, ll)   # External temperature
    if m:
      t = float(m.group(1))

      if t >= 16384:                # Correction for negative numbers representation in diag output
        t = t - 32768

      if t >= 255:
        LogLine("*** Invalid value read in ATTS for ExtT: %4.1f"%t)
      else:
        extT = t

 LastExtT = extT
 LastRoomT = roomT
 LastAuxT = auxT
 return extT, roomT, auxT

# GetBaroP()
def GetBaroP():
 global comR
 BaroP = 0

 s = WaitReplySafe(comR, 'ATBARO')[0]
 t=re.search('.*P:(.+)', s);
 if t:
  BaroP = float(t.group(1));
  BaroP = BaroP * 0.00750061561303	# Convert Pa to mmHg
 else:
  BaroP = 0.
 return BaroP;

# GetRoomTargetT()
def GetRoomTargetT():
 global comC, CfgTemp, TargetTemp

 t=re.search('.*\]=(.+)', WaitReplySafe(comC, 'AT*TGN$')[0]);
 if t:
  r = int(t.group(1))/10.
 else:
  r = 0.
 TargetTemp = r
# print ('Current TT:%f |'+t.group(1))%r
 return r;


# Read external temperature data
def GetExtTempQQ():
 global comC

 Temp_reply = WaitReplySafe(comC, 'ATG');           # Read ext temperature data
 Temp = [];
 for s in Temp_reply:
  t = re.search('.*=(.+)', s)
  if t:
   Temp.append(int(t.group(1)))
 t = re.search('history at (\d+):(\d+):(\d+)\(', Temp_reply[0]);
 h=int(t.group(1));
 m=int(t.group(2));
 s=int(t.group(3));

 return (Temp,h,m,s)


#
# Main html page header
# (with auto-refresh)
#   <!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
#                 <html xmlns="http://www.w3.org/1999/xhtml" lang="en-US" xml:lang="en-US">
#                  <meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1" />

HdrHtml = """
<!DOCTYPE HTML>
                 <head>
		 <title>CondCtl</title>
<meta Http-Equiv="Cache-Control" Content="no-cache">
<meta Http-Equiv="Pragma" Content="no-cache">
<meta Http-Equiv="Expires" Content="0">
<meta Http-Equiv="Pragma-directive: no-cache">
<meta Http-Equiv="Cache-directive: no-cache">
<meta name="viewport" content="width=410">
<meta name="viewport" content="initial-scale=1">
<meta http-equiv="refresh" content="60; url=/cgi-bin/condctl">
		 <head>
		 <body>
"""


#
# Settings page header
#
SettingsHdrHtml = """
  <!DOCTYPE html
   PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
		 <html xmlns="http://www.w3.org/1999/xhtml" lang="en-US" xml:lang="en-US">
		 <head>
		 <title>CondCtl</title>
		 <meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1" />
<meta Http-Equiv="Cache-Control" Content="no-cache">
<meta Http-Equiv="Pragma" Content="no-cache">
<meta Http-Equiv="Expires" Content="0">
<meta Http-Equiv="Pragma-directive: no-cache">
<meta Http-Equiv="Cache-directive: no-cache">
<meta name="viewport" content="width=400">
		 <head>
		 <body>
"""

#
# Main control form template
#
MainFormHtmlTemplate = """

<div style="overflow:scroll; width:1000px;height:350px" >
<table cellpadding=1 cellspacing=1 border=1 style="font-size:0.8em;line-height:1.2em;font-family:monospace">
<tr>
<td><img SRC="/cgi-bin/genimg"></td>


<td>
 <div>

%s

</div>
</td>

</tr>
</table>
</div>

<form>
<table cellpadding=5 cellspacing=5 border=1>
<tr><td>Mode: </td>    <td> %s</td><td><INPUT TYPE=submit NAME='CondCtlMode' VALUE='Off'><INPUT TYPE=submit NAME='CondCtlMode' VALUE='On'></td></tr>
<tr><td>%4.1f/%s%s</td><td>%4.1f</td><td><INPUT TYPE=submit NAME='TargetT' VALUE='-'><INPUT TYPE=submit NAME='TargetT' VALUE='+'><INPUT TYPE=submit NAME='SetT' VALUE='SetT'></td></tr>
<tr><td>Fan: </td><td> %s</td><td><INPUT TYPE=submit NAME='HighFanMode' VALUE='Off'><INPUT TYPE=submit NAME='HighFanMode' VALUE='On'><INPUT TYPE=submit NAME='HighFanMode' VALUE='Forced'></td></tr>
<tr><td>Blinds: </td>  <td> %s</td><td><INPUT TYPE=submit NAME='BlindsMode'  VALUE='88'> <INPUT TYPE=submit NAME='BlindsMode' VALUE='90'><INPUT TYPE=submit NAME='BlindsMode' VALUE='95'><INPUT TYPE=submit NAME='BlindsMode' VALUE='100'></td></tr>
<tr><td><A href="/cgi-bin/settings">Settings</A></td>
<td></td><td></td>
</tr>
</table>
</form>
%s
<p>
%s
"""

def GenerateRoomTargetTSelect():
 r = """<select name="RoomTSelect">"""
 found = False
 for t in [18,19,20.0,20,5,21.0,21.5,22.0,22.5,23.0,23.5,24.0,24.5,25]:
   r += """<option %s>%s</option>"""%("selected" if t == TargetTemp else '', str(t))+"\n"
   if t == TargetTemp:
     found = True
 if not found:
   r += """<option %s>%s</option>"""%("selected", str(t))+"\n"
 r += "</select>"
 return r


#
def GenerateAcSelect():
 r = """<select name="AcSelect">"""
 found = False
 for t in [18,19,20,21,22,23,24,25,26]:
   r += """<option %s>%s</option>"""%("selected" if t == Ac.Calibrate(TargetTemp) else '', str(t))+"\n"
   if t == Ac.Calibrate(TargetTemp):
     found = True
 if not found:
   r += """<option %s>%s</option>"""%("selected", str(t))+"\n"
 r += "</select>"
 return r

FanOnOffTimes = [1, 2, 3, 4, 5, 10, 15, 20, 25, 30, 40, 60]   # Allowed values for periodic ventilation

#
# Generate html to control periodic fan on/off
#
def GenerateCurrentFanOnOffHtmlString():
 r = """Periodic ventilation, On: <select name="FanPOn">"""

 if CfgFanOnTime == 0:    # First special option for "disabled" state
   r += """<option selected>No</option>"""
 else:
   r += """<option>No</option>"""

 for t in FanOnOffTimes:  # Discrete set of selectable on/off times
   r += """<option %s>%s</option>"""%("selected" if CfgFanOnTime == t else '', str(t))+"\n"

 r += "</select>"
 r += """ Off: <select name="FanPOff">"""
 for t in FanOnOffTimes:  # Discrete set of selectable on/off times
   r += """<option %s>%s</option>"""%("selected" if CfgFanOffTime == t else '', str(t))+"\n"
 r += "</select>"
 return r

# Event types and mapping of select strings to them
class EvType:
  Off, On, SetT, SetB88, SetB100 = range(0, 5)

Select2evType = { 'Off':EvType.Off, 'On':EvType.On, 'SetT':EvType.SetT, 'SetB88':EvType.SetB88, 'SetB100':EvType.SetB100 }

class SchedEvent:
  evEnabled = False
  evTime = datetime.time(0, 0)
  evDays = 0x7F
  evType = EvType.Off
  evTemp = 0

  # Execute event
  def Execute(self):
    global comC, comR, TargetTemp

    if self.evType == EvType.Off:
      MasterOff(0,'')

    elif self.evType == EvType.On:
      MasterOn(0,'')

    elif self.evType == EvType.SetT:
      TargetTemp = self.evTemp
      WaitReplySafe(comC, 'AT*TGN=%d'%(TargetTemp*10))
      Ac.SetTemperature(TargetTemp)

    elif self.evType == EvType.SetB88:
      WaitReplySafe(comR, 'ATSRV=88')

    elif self.evType == EvType.SetB100:
      WaitReplySafe(comR, 'ATSRV=100')

  # Generate self-description string for logs
  def GenerateString(self):
    r = "";
    if self.evEnabled:
      r += '[X]'
    else:
      r += '[ ]'

    r += " %02d:%02d"%(self.evTime.hour, self.evTime.minute)
    r += " %02X "%(self.evDays)

    for (s, t) in Select2evType.iteritems():
       if t == self.evType:
          r += s
          break
    r += " %02d"%self.evTemp
    return r

  # Generate self-description test for html form
  def GenerateFormString(self, id):
    r = """<tr>"""
    r += """<td> <input name="EvEn_#id#" type="checkbox"  value="True" %s> Enable</td>"""%("checked" if self.evEnabled else '')+"\n"
    r += """<td> <input name="EvTi_#id#" type="time" value="%02d:%02d"></td>"""%(self.evTime.hour, self.evTime.minute)+"\n"
    r += """<td>"""+"\n"
    t = 1
    for i in range(0, 7):
      r += """<input name="EvD%d_#id#"   type="checkbox"  value="True" %s>"""%(i, "checked" if (self.evDays & t) != 0 else '')+"\n"
      t <<= 1
    r += """</td>"""+"\n"
    r += """<td>"""
    r += """<select name="EvTy_#id#">"""+"\n"

    for s in ['Off','On','SetT','SetB88','SetB100']:	# Use convenient ordering instead of arbitrary dictionary enumeration
      r += """<option %s>%s</option>"""%("selected" if Select2evType[s] == self.evType else '', s)+"\n"
    r += """</select></td>"""+"\n"
    r += """</td>"""+"\n"
    r += """<td>"""+"\n"
    r += """<select name="EvT_#id#">"""+"\n"
    for t in range(16,26):
      r += """<option %s>%d</option>"""%("selected" if self.evType == EvType.SetT and self.evTemp == t else '', t)+"\n"
    r += """</select></td>"""+"\n"
    r += """<td> <button TYPE=submit NAME="Del" value="#id#">Del</button></td>"""+"\n"
    r = r.replace("#id#", str(id))	# Set event index into parameter names
    return r


#
# PrepImg() - Prepage image with temperature graphs etc.
#
def PrepImg():
 global Img, LockImg

 # Generate plot
 sizeX = 400
 sizeY = 240
 sizeYr = 80

 ty0 = 1
 ty1 = sizeY-2

 ry0 = sizeY+4+1
 ry1 = sizeY+sizeYr-2

 def ry(t):
  return ry1-(t-18.)*(ry1-ry0)/(26-18)

 def xy(t):
  return ty1-(t-(-30.))*(ty1-ty0)/(30-(-30))

 with LockImg:

# White version
#  backgr = (255, 255, 255)
#  black = (0,0,0)
#  gridColor = (230,230,230)
#  strobeColor = (0,180,0)
#  lineColor = (0,0,255)
#  lineIColor = (0,200,255)

# Dark version, to align with widgets
  backgr = (13,33,87)
  borderColor = (180,180,180)
  gridColor = (123,123,128) #  gridColor = (86,86,89)
  gridColorHalf =(80,80,82)#  gridColorHalf =(65,65,80)
  lineIColor = (0,200,255)
  pressureColor = (247,137,94)
  gisMeteoColor = (180,180,182)
  timeScaleColor = (250, 250, 250)
  strobeColor = (0,200,0)
  lineColor = (123,222,255)
  lineColorPrev = (43,121,255)
  sunColor = (255, 237, 79)


  Img = Image.new("RGB", (sizeX, sizeY+sizeYr+6), backgr)
  draw = ImageDraw.Draw(Img)


  # Space for external temperatures
  draw.rectangle([0, 0, sizeX-1, sizeY-1], outline=borderColor);
  # Space for internal temperatures
  draw.rectangle([0, sizeY+4, sizeX-1, sizeY+sizeYr-1], outline=borderColor);

  # Draw bar for on/off states
  L = len(ClimateHist)
  for i in range(L-1):
    draw.rectangle([i*sizeX/L, sizeY+sizeYr+1, (i+1)*sizeX/L, sizeY+sizeYr+3], outline=(0,0,245) if ClimateHist[i] else backgr)

  L = len(FanHist)
  for i in range(L-1):
    draw.rectangle([i*sizeX/L, sizeY+sizeYr+4, (i+1)*sizeX/L, sizeY+sizeYr+6], outline=(0,245,0) if FanHist[i] else backgr)

  # Grid for external temperatures
  for i in range(1, 24):
   draw.line([i*sizeX/24, 1, i*sizeX/24, sizeY-2], fill=gridColor, width=1);

  for i in range(-25, 25+1, 10):
   draw.line([1, xy(i), sizeX-2, xy(i)], fill=gridColorHalf, width=1);

  for i in range(-30, 30+1, 10):
   draw.line([1, xy(i), sizeX-2, xy(i)], fill=gridColor, width=1);

  draw.line([0, xy(0), sizeX-1, xy(0)], fill=borderColor, width=1);

  fntScale = ImageFont.truetype('arial.ttf', 15)

  # Time scale
  for h in [9,12,15,18,21]:
    draw.text([h*sizeX/24, sizeY/2], "%2d"%(h), font=fntScale, fill=timeScaleColor)
    draw.line([h*sizeX/24, xy(-2), h*sizeX/24, xy(2)], fill=timeScaleColor, width=1)

  # Grid for room temperatures
  for i in range(18, 26, 1):
   draw.line([2, ry(i), sizeX-2, ry(i)], fill=gridColor, width=1);

  for i in range(1, 24):
   draw.line([i*sizeX/24, ry(18), i*sizeX/24, ry(26)], fill=gridColor, width=1);

  draw.line([1, ry(20), sizeX-2, ry(20)], fill=borderColor, width=1);

  # Plot gisMeteo forecast
  L = len(GisMeteoT)
  for i in range(L-1): 
   draw.line([i*sizeX/L, xy(GisMeteoT[i][1]), (i+1)*sizeX/L, xy(GisMeteoT[i+1][1])], fill=gisMeteoColor, width=1)

  for i in range(L): 
   GisMeteoT[i][2].seek(0)
   icon = Image.open(GisMeteoT[i][2])
   Img.paste(icon, (i*sizeX/L, sizeY*7/8), icon);
   del icon

  # Plot sunrise & sunset lines
  for t in Sun:
    draw.line([t*sizeX/(24*60), 40, t*sizeX/(24*60), sizeY-2], fill=sunColor, width=1)


  # Current time cursor position
  tim = datetime.datetime.now()
  curPos = int((tim.hour*60*60+tim.minute*60+tim.second)*sizeX / (24*60*60))

  draw.line([curPos, 1, curPos, sizeY-2], fill=strobeColor, width=2)
  draw.line([curPos, sizeY+4, curPos, sizeY+sizeYr-2], fill=strobeColor, width=2)

  r = 3
  draw.ellipse([curPos-r, xy(LastExtT)-r, curPos+r, xy(LastExtT)+r], outline=lineColor)


#  for i in range(nSamplesR-1):
#   draw.line([i*sizeX/nSamplesR, sizeY/2-PrevExtT[1][i]*3,
#             (i+1)*sizeX/nSamplesR, sizeY/2-PrevExtT[1][i+1]*3], fill=(190,190,255), width=1);

#  for i in range(nSamplesR-1):
#   draw.line([i*sizeX/nSamplesR, sizeY/2-PrevExtT[0][i]*3,
#             (i+1)*sizeX/nSamplesR, sizeY/2-PrevExtT[0][i+1]*3], fill=(130,130,255), width=1);

  L = len(ExtT)

  for i in range(L-1):  # Previous Ext T
   draw.line([i*sizeX/L, xy(PrevExtT[0][i]), (i+1)*sizeX/L, xy(PrevExtT[0][i+1])], fill=lineColorPrev, width=2)

  for i in range(L-1):  # Current Ext T
   draw.line([i*sizeX/L, xy(ExtT[i]), (i+1)*sizeX/L, xy(ExtT[i+1])], fill=lineColor, width=2)

  for i in range(L):    # Current aux T (AC inlet)
   draw.point([i*sizeX/L, ry(AuxT[i])], fill=(90,121,166))

  for i in range(L-1):  # Target T
   draw.line([i*sizeX/L, ry(TgT[i]), (i+1)*sizeX/L, ry(TgT[i+1])], fill=(247,219,166), width=1)
  for i in range(L-1):  # Target AC T
   draw.line([i*sizeX/L, ry(AcT[i]), (i+1)*sizeX/L, ry(AcT[i+1])], fill=(247,166,166), width=1)
  for i in range(L):    # Averaged T
   draw.point([i*sizeX/L, ry(RoomTavr[i])], fill=(165,91,235))
  for i in range(L):    # Sampled T
   draw.point([i*sizeX/L, ry(RoomT[i])], fill=lineIColor);

  r = 3
  draw.ellipse([curPos-r, ry(LastRoomT)-r, curPos+r, ry(LastRoomT)+r], outline=lineIColor)
  draw.ellipse([curPos-r, ry(LastRoomTavr)-r, curPos+r, ry(LastRoomTavr)+r], outline=(165,91,235))

  # Plot pressure graph on external temperature canvas, calibrated at 760mmHg == -10C
  for i in range(L):
   draw.point([i*sizeX/L, xy((BaroP[i]-760-10))], fill=pressureColor)
   
  fntPressure = ImageFont.truetype('arial.ttf', 20)
  draw.text([sizeX/2+sizeX/4, 2], "%d mmHg"%(LastPressure), font=fntPressure, fill=pressureColor)

  fnt = ImageFont.truetype('arial.ttf', 32)

  # Curent timestamp into upper left corner
  draw.text([2,2], "%02d:%02d:%02d  %4.1f"%(tim.hour, tim.minute, tim.second, LastExtT), font=fnt, fill=lineColor)


#
# Main WSGI application handler
#
def application(environ, start_response):
 global comC, comR, LockC, LockR, LockCfg, RoomT, AuxT, BaroP, ClimateHist, FanHist

 def GetImg():
  global comC, comR, Img, LockImg

  f = cStringIO.StringIO()

  with LockImg:
   Img.save(f, "PNG")

  f.seek(0)
  return [f.getvalue()]

#
# SettingsPage() - Handle settings page, setting global configuration parameters
#
 def SettingsPage():
  global CfgAcCtlEnabled, CfgFanCoolingEnabled, CfgEvents, LockCfg, CfgFanOnTime, CfgFanOffTime, MinutesToFanOn

  # Parse configuration parameters, if any

  params = parse_qs(environ.get('QUERY_STRING', ''))

  if environ['REMOTE_USER'] != '':      # If non-empty user (no control otherwise)

   if 'Add' in params:           # Add new event
     with LockCfg:
       CfgEvents.append(SchedEvent())

   elif 'Del' in params:         # Delete requested
     i = int(params['Del'][0])
     with LockCfg:
       del CfgEvents[i]

   elif 'Save' in params:        # Save requested
     with LockCfg:
       if 'ControlAC' in params:
         CfgAcCtlEnabled = True
       else:
         CfgAcCtlEnabled = False

       if 'FanCooling' in params:
         CfgFanCoolingEnabled = True
       else:
         CfgFanCoolingEnabled = False

       if ("FanPOn" in params) and ("FanPOff" in params):
         value = escape(params["FanPOn"][0])
         if value == "No":
           CfgFanOnTime = 0
           CfgFanOffTime = 0
         else:
           if int(value) in FanOnOffTimes:
             CfgFanOnTime = int(value)
             value = escape(params["FanPOff"][0])
             if int(value) in FanOnOffTimes:
               CfgFanOffTime = int(value)
             else:
               CfgFanOnTime = 0
               CfgFanOffTime = 0
         print CfgFanOnTime, CfgFanOffTime
         if CfgFanOnTime != 0:
           MinutesToFanOn = CfgFanOffTime

       for i in range(len(CfgEvents)):
         CfgEvents[i].evEnabled = False
         CfgEvents[i].evDays = 0

       for key in params:
         m = re.search('(.*)_(.*)', key)
         if m:                   # Match -- "indexed" name, must belong to scheduler event
           name = m.group(1)
           i = int(m.group(2))
           if i >= len(CfgEvents):
             break
           if name == 'EvEn':
             CfgEvents[i].evEnabled = True
           else:
             val = escape(params[key][0])
             if name == 'EvTi':
               m2 = re.search('(.*):(.*)', val)
               if m2:
                 CfgEvents[i].evTime = datetime.time(int(m2.group(1)), int(m2.group(2)))
             elif name == 'EvTy':
               if val in Select2evType:
                 CfgEvents[i].evType = Select2evType[val]
               else:
                 CfgEvents[i].evType = EvType.Off
             elif name == 'EvT':
               for t in range(16,26):
                 if str(t) == val:
                   CfgEvents[i].evTemp = t
             else:
               m = re.search('EvD(.*)_(.*)', key)
               if m:
                 CfgEvents[i].evDays |= 1 << int(m.group(1))

     # Sort events by time
     CfgEvents.sort(cmp=lambda x,y:cmp(x.evTime, y.evTime))

     # Saving parameters, save active configuration to file
     sh = shelve.open('condsrv',writeback=True)
     sh['CfgAcCtlEnabled'] = CfgAcCtlEnabled
     sh['CfgFanCoolingEnabled'] = CfgFanCoolingEnabled
     sh['CfgEvents'] = CfgEvents
     sh['CfgFanOnTime'] = CfgFanOnTime
     sh['CfgFanOffTime'] = CfgFanOffTime
     sh.close()

  # Generate HTML page, using current configuration
  r = [SettingsHdrHtml]

  # Generate form
  r.append("""<form>""")
  r.append("""<table cellpadding=2 cellspacing=2 border=1><tr>""")
  r.append("""<td><input type="checkbox" name="ControlAC" value="On" %s> Control AC"""%("checked" if CfgAcCtlEnabled else '')+"</td>\n")
  r.append("""<td><input type="checkbox" name="FanCooling" value="On" %s> Use fan for cooling"""%("checked" if CfgFanCoolingEnabled else '')+"</td>\n")
  r.append("<td>"+GenerateCurrentFanOnOffHtmlString()+"</td></tr>")
  r.append("""<table cellpadding=2 cellspacing=2 border=1>""")
  r.append("""<tr>Timed events</tr>""")
  for i, ev in enumerate(CfgEvents):
    r.append(ev.GenerateFormString(i))
  r.append("""</table>""")
  r.append("""<INPUT TYPE=submit NAME="Add" value="Add">""")
  r.append("""<INPUT TYPE=submit NAME="Save" value="Save">""")
  r.append("""</form>""")
  return r


 def CondCtl():
  global comC, comR, LockCfg, CfgAcCtlEnabled, TargetTemp, CfgFanOnTime, CfgFanOffTime

  def TargetTInc(com, cmd):
   global TargetTemp
   t = GetRoomTargetT()
   t = t + 0.5
   TargetTemp = t
   WaitReplySafe(com, 'AT*TGN=%d'%(t*10))
   Ac.SetTemperature(TargetTemp)

  def TargetTDec(com, cmd):
   global TargetTemp
   t = GetRoomTargetT()
   t = t - 0.5
   TargetTemp = t
   WaitReplySafe(com, 'AT*TGN=%d'%(t*10))
   Ac.SetTemperature(TargetTemp)

  commandMap = [
   ('CondCtlMode', 'On',    MasterOn,   '',	   0),
   ('CondCtlMode', 'Off',   MasterOff,  '',	   0),
   ('TargetT',     '+',     TargetTInc, '',        comC),
   ('TargetT',     '-',     TargetTDec, '',        comC),
   ('HighFanMode', 'On',    FanCtl,     'AT*ENH=1', comC),
   ('HighFanMode', 'Off',   FanCtl,     'AT*ENH=0', comC),
   ('HighFanMode', 'Forced',FanCtl,     'AT*ENH=2', comC),
   ('LowFanMode',  'On',    WaitReplySafe, 'AT*ENL=1', comC),
   ('LowFanMode',  'Off',   WaitReplySafe, 'AT*ENL=0', comC),
   ('LowFanMode',  'Forced',WaitReplySafe, 'AT*ENL=2', comC),
   ('BlindsMode',  '88',    WaitReplySafe, 'ATSRV=88', comR),
   ('BlindsMode',  '90',    WaitReplySafe, 'ATSRV=90', comR),
   ('BlindsMode',  '95',    WaitReplySafe, 'ATSRV=95', comR),
   ('BlindsMode',  '100',   WaitReplySafe, 'ATSRV=100',comR)]

  # Do configuration if requested (if parameters are present in query string)
  params = parse_qs(environ.get('QUERY_STRING', ''))

  if environ['REMOTE_USER'] != '':

   for t in commandMap:
     (name, value, fnc, cmd, port) = t
     if name in params:
      if value == escape(params[name][0]):
       fnc(port, cmd)

   if 'SetT' in params:

     if 'AcSelect' in params:
       Ac.ResetCalibration(int(escape(params['AcSelect'][0])))

     if 'RoomTSelect' in params and not ('TargetT' in params):
       GetRoomTargetT()
       t = float(escape(params['RoomTSelect'][0]))
       if t != TargetTemp:
         TargetTemp = t
         WaitReplySafe(comC, 'AT*TGN=%d'%(t*10))
         Ac.SetTemperature(TargetTemp)

#  if 'LightMode' in form:
#   if form['LightMode'].value == 'Pulse':
#    WaitReply(comR, 'ATIRPM=2000');
#    WaitReply(comR, 'ATDLY=500');
#    WaitReply(comR, 'ATIRPM=200');

  # Prepare memory usage line
  memUsage = "Memory usage: %s kB"%resource.getrusage(resource.RUSAGE_SELF).ru_maxrss 

  # Read bandwidth monitor log file, include last line from it
  fileHandle = open('/root/ckbw.log',"r")
  lineList = fileHandle.readlines()
  fileHandle.close()
  if len(lineList) >= 1:
   recentRate = lineList[len(lineList)-1]
  else:
   recentRate = '<No data>'

  # Prepare html header and the rest of the page
  r = [HdrHtml]		# Output html header
  xt,rt,ft = GetTemperatures()

  recentLogLinesFmt = ""
  for ll in RecentLogLines:
    recentLogLinesFmt += ll + "<br />"

  r.append(MainFormHtmlTemplate%(recentLogLinesFmt,
                                 GetCurrentMode(),
                                 rt, GenerateRoomTargetTSelect(), GenerateAcSelect(), xt,
                                 GetCurrentHighFanMode(),
                                 GetCurrentBlindsMode(),
                                 memUsage,
				 recentRate))
  # Sync time with CondCtl
  t=time.localtime()
  WaitReplySafe(comC, "AT*TMH=%d"%t.tm_hour)
  WaitReplySafe(comC, "AT*TMM=%d"%t.tm_min)
  WaitReplySafe(comC, "AT*TMS=%d"%t.tm_sec)
  WaitReplySafe(comC, "AT*TMW=%d"%t.tm_wday)
  return r

########## Main web server entry

 path = environ.get('PATH_INFO', '').lstrip('/')
 r =["N/A"]

 if path == 'cgi-bin/genimg':
  start_response('200 OK', [('Content-type', 'image/png')])
  r = GetImg()
  return r

 elif path == 'cgi-bin/settings':
  start_response('200 OK', [('Content-Type','text/html; charset=ISO-8859-1')])
  r = SettingsPage()
  return r

 elif path == 'cgi-bin/condctl':
  start_response('200 OK', [('Content-Type','text/html; charset=ISO-8859-1')])
  r = CondCtl()
  return r

 else:
  start_response('404 NOT FOUND', [('Content-Type', 'text/html; charset=ISO-8859-1')])
  return ['Not Found']


def FindPort(name, rate):
 pdir = glob.glob(name)
 com = None
 s = "<None>"
 for s in pdir:
  try:
   com = OpenPort(s, rate);
  except serial.serialutil.SerialException:
   LogLine("...failed opening port at \"%s\""%s)
   com = None
  else:
   break
 return (com, s)


def average(RoomT, ndx):
  a = 0.
  for i in range(int(HistLen / 24 /2)): # Averaging for 0.5 hrs period
    i1 = ndx-i
    if i1 < 0:  # Wrap around
      i1 += HistLen
    a += RoomT[i1]
  return a / (HistLen / 24 /2.)


# ===========================================
# Service thread class
#  -- gathering room temperature statistics, scheduler, other service actions
# ===========================================
class ServiceThreadClass(threading.Thread):
 def run(self):
  global LockCfg, RoomT, RoomTavr, PrevExtT, ClimateHist, ClimateOn, FanHist, TgT, AcT, FanOn, comC, MinutesToFanOn, CfgFanOnTime, CfgFanOffTime
  global LastRoomTavr, AcCalibration, AcMinutesToCheck
  global GisMeteoT, Sun, LastPressure

  LogLine("Service thread started")

  recentDay = datetime.datetime.now().day
  recentTime = datetime.datetime.now()
  recentGisMeteoSampling = recentTime
  recentSampling = -1

  at = 20.       # Current aver. temp (initialize for initial copy propagation)
  at1 = 20.      # Previous average temp samplinggg

  minutesToFanOff = 0

  tim = datetime.datetime.now()
  lastNdx = int(round((tim.hour * 60 + tim.minute + tim.second/60.)/2.))

  # Main loop, each minute
  while 1:

    # Handle scheduler events
    newTime = datetime.datetime.now()
    today = datetime.date.today()
    with LockCfg:
      for evt in CfgEvents:
         if evt.evEnabled and ((1 << newTime.weekday()) & evt.evDays) != 0:
           eventTime = datetime.datetime.combine(today, evt.evTime)
           if eventTime > recentTime and eventTime <= newTime:
              LogLine("Event triggered: "+evt.GenerateString())
              evt.Execute()

    recentTime = newTime

    # Handle fan periodic forced on/off
    with LockCfg:       # Note that MinutesToFanOn global is used to control the subsystem
     if MinutesToFanOn != 0:
       MinutesToFanOn -= 1
       if MinutesToFanOn == 0:
         FanCtl(comC, 'AT*ENH=2')
         minutesToFanOff = CfgFanOnTime+1  # +1, since it is decremented immediately below
         LogLine("Fan Forced")

     if minutesToFanOff != 0:
       minutesToFanOff -= 1
       if minutesToFanOff == 0:
         if CfgFanCoolingEnabled:
           FanCtl(comC, 'AT*ENH=1')     # Set default "on" mode for CondCtl device, enabling it to use for cooling as needed
           LogLine("Fan On")
         else:
           FanCtl(comC, 'AT*ENH=0')     # Just switch off, fan used for this periodic ventilation only
           LogLine("Fan Off")

         MinutesToFanOn = CfgFanOffTime # Start timeout till switching on


    # Handle periodic sampling for graphs and AC adjustment
    #  Each 2 minutes:
    newSampling = datetime.datetime.now()
    if recentSampling == -1 or (newSampling - recentSampling).total_seconds() >= 60*2:
      recentSampling = newSampling

      # Current sampling time & index in arrays
      tim = datetime.datetime.now()
      ndx = (tim.hour * 60 + tim.minute + tim.second/60.)/2.
      ndx = int(ndx) # Note: floor, w/o rounding so that we won't overrun the array size.
                     # Note also: can overwrite or skip samples, depending on timing

      # Sample parameters
      xt,rt,ft = GetTemperatures()
      p = GetBaroP()
      LastPressure = p

      # Get moving average over room temperature history
      at1 = at
      at = average(RoomT, ndx)
      LastRoomTavr = at

      # Handle AC calibration adjustment
      Ac.AdjustCalibration(at, at1, TargetTemp)
      ac = Ac.Calibrate(TargetTemp)     # AC-calibrated target
      LogLine("T: %4.1f, RT: %4.1f, AT: %4.1f, AC: %2d, IT: %4.1f, P: %.3f, M: %s"%(xt, rt, at, Ac.Calibrate(TargetTemp), ft, p, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))

      # Write samples into history arrays, pre-generate graphs
      RoomT[ndx] = rt
      AuxT[ndx] = ft
      ExtT[ndx] = xt

      TgT[ndx] = TargetTemp if ClimateOn else 0
      AcT[ndx] = ac if ClimateOn and CfgAcCtlEnabled else 0

      ClimateHist[ndx] = ClimateOn
      FanHist[ndx] = FanOn
      BaroP[ndx] = p
      RoomTavr[ndx] = at

      expNdx = (lastNdx + 1) % len(RoomT)
      if expNdx != ndx:        # If we skipped one entry due to time slippage -- fill the skipped one
        RoomT[expNdx] = rt
        AuxT[expNdx] = ft
        ExtT[expNdx] = xt
        TgT[expNdx] = TargetTemp if ClimateOn else 0
        AcT[expNdx] = ac if ClimateOn and CfgAcCtlEnabled else 0
        RoomTavr[expNdx] = at

        ClimateHist[expNdx] = ClimateOn
        FanHist[expNdx] = FanOn
        BaroP[expNdx] = p

      lastNdx = ndx

      PrepImg()         # Pre-generate temperatures plot

      # Upload image to external hosting
      f = cStringIO.StringIO()
      with LockImg:
        Img.save(f, "PNG")
      f.seek(0)

      try: 
        ftp = ftplib.FTP(FtpServer)
        ftp.login(FtpLogin, FtpPassword)
        ftp.set_pasv(True)	# Simplistic server supports only pasive mode

        # Deglitch for simplistic Windows server
        if len(FtpFileName) >= 3:
          if FtpFileName[0] == '/' and FtpFileName[2] == '/': # Make sure we have single-lettered first "folder"
            ftp.cwd(FtpFileName[0:2])	# Switch to that folder

        ftp.storbinary('STOR '+FtpFileName, f)
        ftp.close()
        f.close()
      except:
        print "*** FTP Error:", sys.exc_info()[0]


      # After midnight, once: Dump external temperature for the last 24hrs, shift last days graphs
      if recentDay != datetime.datetime.now().day:
        recentDay = datetime.datetime.now().day

        PrevExtT[1] = PrevExtT[0][:]
        PrevExtT[0] = ExtT[:]
        logT = open('/www/cgi-bin/ext_temp.log',"a+")
        strTemp = map(lambda(x): str(x)+' ', ExtT)
        logT.write("["+DateTime()+"] ")
        logT.writelines(strTemp[0:len(strTemp)])
        logT.write('\n')
        logT.close()

    # Handle GisMeteo polling
    #  Each 20 minutes:
    newGisMeteoSampling = datetime.datetime.now()
    if (newGisMeteoSampling - recentGisMeteoSampling).total_seconds() >= 60*20:
      recentGisMeteoSampling = newGisMeteoSampling      
      (GisMeteoT, Sun) = ReadGisMeteo()
      LogLine("GisMeteo hourly: "+','.join(str(item) for innerlist in GisMeteoT for item in innerlist))

    time.sleep(60) # Sleep for 1 minute


def authfunc(environ, realm, username):
  """Digest Authentication function"""

  if username == UserName:
    return digest_password(realm, username, UserPass)
#  elif username == '':
#    return digest_password(realm, username, '')
  else:
    LogLine("Access denied: from %s, user: \"%s\""%(environ['REMOTE_ADDR'], username))
    return 0


def ReadGisMeteoPage(url):
 """page=ReadGisMeteoPage(url)
  Read page from the given URL
 """

 if url[0:2] == "//":
   url = "http:"+url	# Add prefix if missing

 r = ""
 try: 
   opener = urllib2.build_opener()

   headers = {
     'User-Agent': 'Mozilla/5.0 (Windows NT 5.1; rv:10.0.1) Gecko/20100101 Firefox/10.0.1',
   }

   opener.addheaders = headers.items()
   response = opener.open(url)
   r = response.read()
 except:
   print "*** URLlib Error:", sys.exc_info()[0]

 return r


def ReadGisMeteo():
 """(gisT, sun)=ReadGisMeteo()
  Read array of hour:temperatire and sun[2] (sunrise, sunset, in minutes)
 """

 page=ReadGisMeteoPage("https://www.gismeteo.ru/city/hourly/4079")


 # Read prognosis for array of hours for today
 d = time.strftime("%Y-%m-%d")
 hours = [0,3,6,9,12,15,18,21]

 gisT = [[0 for i in range(3)] for j in range(len(hours))] 
 i = 0
 for h in hours:
   r=re.search("Local: "+d+" "+str(h)+":00.*?img class=\"png\" src=\"(.*?)\".*?m_temp c'>(.*?)([0-9]+)", page, re.DOTALL)
   if r:
     t = int(r.group(3))
     if 'minus' in r.group(2):
       t = -t
  
     gisT[i][0] = h	# Save hour
     gisT[i][1] = t	# Save temperature
     gisT[i][2] = cStringIO.StringIO(ReadGisMeteoPage(r.group(1)))  # Save weather icon 
     i = i + 1

 # Read sunrise/sunset for today
 sun = [0, 0]
 r = re.search("astronomy_value\">(\d\d:\d\d)</b>.*?astronomy_value\">(\d\d:\d\d)</b>", page, re.DOTALL)
 if r:
   t = r.group(1)
   minutes = int(t[0:2])*60+int(t[3:5])
   sun[0] = minutes
   t = r.group(2)
   minutes = int(t[0:2])*60+int(t[3:5])
   sun[1] = minutes

     
 return (gisT, sun)



# ===========================================
#  Main application, starting WSGI server
# ===========================================

RecentLogLines = ["" for i in range(26)]

# Open log file
LogHandle = open('/www/cgi-bin/condsrv.log','w')

LogLine("** CondSrv home CondCtl and other peripherals WSGI server **")

# Create lock objects for accessing comC, comR and configuration state
LockC = threading.RLock()
LockR = threading.RLock()
LockCfg = threading.RLock()
LockImg = threading.RLock()

# Create lists for various measurements
RoomT = [20. for i in range(HistLen)]
RoomTavr = [20. for i in range(HistLen)]
ExtT = [2. for i in range(HistLen)]
AuxT = [0. for i in range(HistLen)]
BaroP = [0. for i in range(HistLen)]
ClimateHist   = [False for i in range(HistLen)]
FanHist = [False for i in range(HistLen)]
PrevExtT = [[20. for i in range(HistLen)] for j in range(2)]
TgT = [21. for i in range(HistLen)]
AcT = [22. for i in range(HistLen)]
GisMeteoT = [[0 for i in range(3)] for j in range(8)] 


# Read global configuration
LogLine("Loading system configuration from condsrv.cfg")
config = ConfigParser.ConfigParser()
config.read('condsrv.cfg')
RoomTstring = config.get('Thermometers','RoomTstring')
AuxTstring = config.get('Thermometers','AuxTstring')
ExtTstring = config.get('Thermometers', 'ExtTstring')

UserName = config.get('Users','Name','')
UserPass = config.get('Users','Password','')

FtpServer    = config.get('FTP','Server','')
FtpLogin     = config.get('FTP','Login','')
FtpPassword  = config.get('FTP','Password','')
FtpFileName  = config.get('FTP','FileName','')


# Read GUI configuration variables from the database
LogLine("Loading configuration from condsrv.dat")
sh = shelve.open('condsrv')
if 'CfgAcCtlEnabled' in sh:
  CfgAcCtlEnabled = sh['CfgAcCtlEnabled']
  LogLine(" global state loaded")
else:
  CfgAcCtlEnabled = False
  LogLine(" no global state loaded")

if 'CfgFanCoolingEnabled' in sh:
  CfgFanCoolingEnabled = sh['CfgFanCoolingEnabled']
else:
  CfgFanCoolingEnabled = False

if 'CfgEvents' in sh:
  CfgEvents=sh['CfgEvents']
  LogLine(" scheduler events loaded")
else:
  CfgEvents=[]
  LogLine(" no scheduler events loaded")

if ('CfgFanOnTime' in sh) and ('CfgFanOffTime' in sh):
  CfgFanOnTime = sh['CfgFanOnTime']
  CfgFanOffTime = sh['CfgFanOffTime']

if CfgFanOnTime != 0:
  MinutesToFanOn = CfgFanOffTime

sh.close()

Img = Image.new("RGB", (50, 50), "#4F4F4F")


# Load previous day external temperatures from log
LogLine("Loading previous day temperatures")
f = open('/www/cgi-bin/ext_temp.log',"r")
lineList = f.readlines()
f.close()
if len(lineList) >= 1:
  s = lineList[len(lineList)-1]
t = re.search('\[.*\] (.*) $', s)
if t:
  t = t.group(1).split(' ')
  PrevExtT[0] = map(lambda(x): float(x), t)
  LogLine("Previous day temperatures loaded")
else:
  LogLine("No previous day temperatures loaded")
#
#if len(lineList) >= 2:
#  s = lineList[len(lineList)-2]
#t = re.search('\[.*\] (.*) $', s)
#if t:
#  t = t.group(1).split(' ')
#  PrevExtT[1] = map(lambda(x): int(x), t)

# Find and open peripheral com ports
(comC, nameC) = FindPort("/dev/ttyUSB*", 38400)
(comR, nameR) = FindPort("/dev/ttyACM*", 9600)

if comC:
 LogLine("CondCtl port opened at \"%s\", 38400"%nameC)
else:
 LogLine("Cannot open CondCtl at ttyUSB*, exiting")
 sys.exit("*** Cannot open CondCtl at ttyUSB*, exiting")

if comR:
 LogLine("Arduino port opened at \"%s\", 9600"%nameR)
else:
 LogLine("Cannot open Arduino at ttyACM*, exiting")
 sys.exit("*** Cannot open Arduino at ttyACM*, exiting")

time.sleep(2);					# Arduino de-glitching/startup

WaitReplySafe(comC, "AT*ECB=200")

# Read current target temperature and mode from CondCtl device, sync state
GetRoomTargetT()
LogLine("Target temperature read: %4.1f (AC: %d)"%(TargetTemp, Ac.Calibrate(TargetTemp)))

if GetCurrentMode() == 'On':
  MasterOn(0, 0)
  LogLine("CondCtl On, setting Master On")
else:
  MasterOff(0, 0)
  LogLine("CondCtl Off, setting Master Off")

FanOn = GetCurrentHighFanMode() == 'Forced'

# Read GisMeteo forecast
(GisMeteoT, Sun) = ReadGisMeteo()
LogLine("GisMeteo hourly: "+','.join(str(item) for innerlist in GisMeteoT for item in innerlist))
LogLine("Sun minutes: "+str(Sun[0])+", "+str(Sun[1]))

#pdb.set_trace()

#
# Start service thread for internal room statistics and aux control
#
ServiceThread = ServiceThreadClass()
ServiceThread.start()

#
# Create and start web server
#
private_wrapped = AuthDigestHandler(application, "Private area", authfunc)

srv = make_server('10.0.0.126', 80, private_wrapped)
LogLine("Starting web server...")

srv.serve_forever()

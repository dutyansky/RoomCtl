#!/usr/bin/python
#
# CondSrv.py -- Web inteface and high-level controls for miscellaneous apartment
#  housekeping.
#
# See RoomCtl.ino for Arduino sketch.
#
# Assumes Python 2.7.3, currently run on OpenWRT router box
#

import time
import serial
import re
import Image,ImageDraw
import cStringIO
import datetime
import threading
import sys
import glob
import shelve

sys.stderr = sys.stdout

import os, errno

import pdb

from cgi import parse_qs, escape
from wsgiref.simple_server import (make_server, WSGIRequestHandler)

#
# Global configuration parameters
#
CfgAcCtlEnabled = False	# AC control enabled
CfgFanOnTime = 0        # Fan on forced time, 0=Disabled
CfgFanOffTime = 0       # Fan off forced time
CfgEvents = []          # Scheduler events

#
# Global state variables
#
ExtT = []               # List of external temperatures
PrevExtT = []           # List of previous days external temperatures
RoomT = []              # List of room temperatures for current day
BaroP = []              # List of baro pressures for current day
FridgeT = []            # List of fridge temperatures for current day
ClimateHist = []        # List of climate on/off states
FanHist = []            # List of fan on/off forced states
LastExtT, LastRoomT, LastFridgeT = 0,0,0 # Recent temperature measurements
ClimateOn = False       # "Climate control on" state
FanOn = False           # Current low-level "forced fan" flag
TargetTemp = 0          # Target room temperature
MinutesToFanOn = 0
Img = []

LogHandle = 0

def LogLine(s):
 s1 = "["+DateTime()+"] "+s
 LogHandle.write(s1+'\n')
 LogHandle.flush()
 print(s1)

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
 time.sleep(2)
 if re.search(".*ttyUSB.*", x.name):
   (comC, nameX) = FindPort("/dev/ttyUSB*", 38400)
 else:
   (comR, nameX) = FindPort("/dev/ttyACM*", 9600)
 time.sleep(2);
 LogLine("Error encountered on \""+x.name+"\", reconnected as \""+nameX+"\"");


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

#   if s and not re.search('^Temp\[*', s):
#    sys.stdout.write("Re:\""+s+"\"\r\n");

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
  WaitReply(com, s)
  FanOn = s == 'AT*ENH=2'       # Set to True if forced on

#
# MasterOff() - Disable all climate control functions
# Inputs:
#
#       Ignored
#
def MasterOff(com, s):
  global comC, comR, CfgAcCtlEnabled, ClimateOn
  WaitReply(comC, 'AT*MOD=0')	 # Switch CondCtl off
  ClimateOn = False
  if CfgAcCtlEnabled:
    WaitReply(comR, 'ATAC=0') # Switch AC off

#
# MasterOn() - Enable all climate control functions
# Inputs:
#
#       Ignored
#
def MasterOn(com, s):
  global comC, comR, CfgTemp, CfgAcCtlEnabled, TargetTemp, ClimateOn
  WaitReply(comC, 'AT*MOD=3')    # Switch CondCtl on
  ClimateOn = True
  if CfgAcCtlEnabled:
    WaitReply(comR, 'ATAC=%d'%int(TargetTemp)) # Switch AC on, set temperature

#
# Helper functions for getting various peripheral states
#
def GetCurrentMode():
 global comC
 t=re.search('.*\]=(.+)', WaitReply(comC, 'AT*mod$')[0]);
 if t:
  r = 'On' if t.group(1) == '3' else 'Off'
 else:
  r = 'N/A'
 print 'Current Mode:'+r;
 return r;

def GetCurrentHighFanMode():
 global comC
 t=re.search('.*\]=(.+)', WaitReply(comC, 'AT*ENH$')[0]);
 if t:
  r = 'Forced' if t.group(1) == '2' else 'On' if t.group(1) == '1' else 'Off'
 else:
  r = 'N/A'
 print 'Current HF Mode:'+r;
 return r;

def GetCurrentLowFanMode():
 global comC
 t=re.search('.*\]=(.+)', WaitReply(comC, 'AT*ENL$')[0]);
 if t:
  r = 'Forced' if t.group(1) == '2' else 'On' if t.group(1) == '1' else 'Off'
 else:
  r = 'N/A'
 print 'Current LF Mode:'+r;
 return r;

def GetCurrentBlindsMode():
 global comR
 s = WaitReply(comR, 'ATSRVR')[0]
 t=re.search('.*:(.+)', s);
 if t:
  r = t.group(1);
 else:
  r = 'N/A'
 print 'Current Blinds Mode:'+r;
 return r;


# Return current temperatures
# xt,rt,ft = GetTemperatures()
def GetTemperatures():
 global comC, LastExtT, LastRoomT, LastFridgeT
 extT = 0
 roomT = 0
 fridgeT = 0

 for i in range(3):
   try:
     l = WaitReply(comC, "ATTS")
     if re.search('Key', l[0]):
       break
   except PortError, x:
     Reconnect(x)

 for i in range(len(l)):
   m = re.search('30274600000056.*T=(\d*\.\d*)',l[i])   # Room temperature
   if m:
     t = float(m.group(1))
     roomT = t

   m = re.search('AA8E5B02080074.*T=(\d*\.\d*)',l[i])   # Fridge temperature (now just outside)
   if m:
     t = float(m.group(1))
     fridgeT = t

   m = re.search('7C519F0008004B.*T=(\d*\.\d*)',l[i])   # External temperature
   if m:
     t = float(m.group(1))
     extT = t

 LastExtT = extT
 LastRoomT = roomT
 LastFridgeT = fridgeT
 return extT, roomT, fridgeT

# GetBaroP()
def GetBaroP():
 global comR
 BaroP = 0

 s = WaitReply(comR, 'ATBARO')[0]
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

 t=re.search('.*\]=(.+)', WaitReply(comC, 'AT*TGN$')[0]);
 if t:
  r = int(t.group(1))/10.
 else:
  r = 0.
 TargetTemp = r
 print ('Current TT:%f |'+t.group(1))%r
 return r;


# Read external temperature data
def GetExtTempQQ():
 global comC

 Temp_reply = WaitReply(comC, 'ATG');		# Read ext temperature data
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
#
HdrHtml = """
  <!DOCTYPE html
   PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN"
		 "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
		 <html xmlns="http://www.w3.org/1999/xhtml" lang="en-US" xml:lang="en-US">
		 <head>
		 <title>CondCtl</title>
		 <meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1" />
<meta Http-Equiv="Cache-Control" Content="no-cache">
<meta Http-Equiv="Pragma" Content="no-cache">
<meta Http-Equiv="Expires" Content="0">
<meta Http-Equiv="Pragma-directive: no-cache">
<meta Http-Equiv="Cache-directive: no-cache">
<meta name="viewport" content="width=410">
<meta http-equiv="refresh" content="60; url=/cgi-bin/condctl">
		 <head>
		 <body>
"""

#
# Settings page header
#
SettingsHdrHtml = """
  <!DOCTYPE html
   PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN"
		 "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
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
<img SRC="/cgi-bin/genimg">
<form>
<table cellpadding=5 cellspacing=5 border=1>
<tr><td>Mode: </td>    <td> %s</td><td><INPUT TYPE=submit NAME='CondCtlMode' VALUE='Off'><INPUT TYPE=submit NAME='CondCtlMode' VALUE='On'></td></tr>
<tr><td>T:%4.1f/%4.1f,%d</td><td>%4.1f</td><td><INPUT TYPE=submit NAME='TargetT' VALUE='-'><INPUT TYPE=submit NAME='TargetT' VALUE='+'></td></tr>
<tr><td>Fan: </td><td> %s</td><td><INPUT TYPE=submit NAME='HighFanMode' VALUE='Off'><INPUT TYPE=submit NAME='HighFanMode' VALUE='On'><INPUT TYPE=submit NAME='HighFanMode' VALUE='Forced'></td></tr>
<tr><td>Blinds: </td>  <td> %s</td><td><INPUT TYPE=submit NAME='BlindsMode'  VALUE='88'> <INPUT TYPE=submit NAME='BlindsMode' VALUE='90'><INPUT TYPE=submit NAME='BlindsMode' VALUE='95'><INPUT TYPE=submit NAME='BlindsMode' VALUE='100'></td></tr>
<tr><td><A href="/cgi-bin/settings">Settings</A></td>
<td></td><td></td>
</tr>
</table>
</form>
%s
"""


FanOnOffTimes = [1, 2, 5, 10, 20, 30, 40, 60]   # Allowed values for periodic ventilation

#
# Generate html to control periodic fan on/off
#
def GenerateCurrentFanOnOffHtmlString():
 r = """Periodic ventilation, On: <select name="FanPOn">"""

 if CfgFanOnTime == 0:       # First option, for "Not enabled" state
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
    global comC, comR, TargetTemp, CfgAcCtlEnabled

    if self.evType == EvType.Off:
      MasterOff(0,'')

    elif self.evType == EvType.On:
      MasterOn(0,'')

    elif self.evType == EvType.SetT:
      TargetTemp = self.evTemp
      WaitReply(comC, 'AT*TGN=%d'%(TargetTemp*10))
      if CfgAcCtlEnabled:
         WaitReply(comR, 'ATAC=%d'%int(TargetTemp)) # Switch AC on, set temperature

    elif self.evType == EvType.SetB88:
      WaitReply(comR, 'ATSRV=88')
    elif self.evType == EvType.SetB100:
      WaitReply(comR, 'ATSRV=100')

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



def PrepImg():
 global Img, LockImg

#  (Temp,h,m,s) = GetExtTemp()

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

 def ty(t):
  return ty1-(t-(-30.))*(ty1-ty0)/(30-(-30))

 with LockImg:
  Img = Image.new("RGB", (sizeX, sizeY+sizeYr+6), "#FFFFFF")
  draw = ImageDraw.Draw(Img)

  white = (255,255,255)
  black = (0,0,0)
  gridColor = (230,230,230)
  strobeColor = (0,180,0)
  lineColor = (0,0,255)
  lineIColor = (0,200,255)

  # Space for external temperatures
  draw.rectangle([0, 0, sizeX-1, sizeY-1], outline=black);
  # Space for internal temperatures
  draw.rectangle([0, sizeY+4, sizeX-1, sizeY+sizeYr-1], outline=black);

  # Draw bar for on/off states
  L = len(ClimateHist)
  for i in range(L-1):
    draw.rectangle([i*sizeX/L, sizeY+sizeYr+1, (i+1)*sizeX/L, sizeY+sizeYr+3], outline=(0,0,245) if ClimateHist[i] else white)

  L = len(FanHist)
  for i in range(L-1):
    draw.rectangle([i*sizeX/L, sizeY+sizeYr+4, (i+1)*sizeX/L, sizeY+sizeYr+6], outline=(0,245,0) if FanHist[i] else white)

  # Grid for external temperatures
  for i in range(1, 24):
   draw.line([i*sizeX/24, 1, i*sizeX/24, sizeY-2], fill=(245,245,245), width=1);

  for i in range(-25, 25+1, 10):
   draw.line([1, ty(i), sizeX-2, ty(i)], fill=(245,245,245), width=1);

  for i in range(-30, 30+1, 10):
   draw.line([1, ty(i), sizeX-2, ty(i)], fill=gridColor, width=1);

  draw.line([0, ty(0), sizeX-1, ty(0)], fill=black, width=1);

  # Grid for room temperatures
  for i in range(18, 26, 1):
   draw.line([2, ry(i), sizeX-2, ry(i)], fill=gridColor, width=1);

  for i in range(1, 24):
   draw.line([i*sizeX/24, ry(18), i*sizeX/24, ry(26)], fill=gridColor, width=1);

  draw.line([1, ry(20), sizeX-2, ry(20)], fill=black, width=1);

  # Current time cursor position
  tim = datetime.datetime.now()
  curPos = (tim.hour*60*60+tim.minute*60+tim.second)*sizeX / (24*60*60)

  draw.line([curPos, 1, curPos, sizeY-2], fill=strobeColor, width=1)
  draw.line([curPos, sizeY+4, curPos, sizeY+sizeYr-2], fill=strobeColor, width=1)

  r = 3
  draw.ellipse([curPos-r, ty(LastExtT)-r, curPos+r, ty(LastExtT)+r], outline=lineColor)


#  for i in range(nSamplesR-1):
#   draw.line([i*sizeX/nSamplesR, sizeY/2-PrevExtT[1][i]*3,
#             (i+1)*sizeX/nSamplesR, sizeY/2-PrevExtT[1][i+1]*3], fill=(190,190,255), width=1);

#  for i in range(nSamplesR-1):
#   draw.line([i*sizeX/nSamplesR, sizeY/2-PrevExtT[0][i]*3,
#             (i+1)*sizeX/nSamplesR, sizeY/2-PrevExtT[0][i+1]*3], fill=(130,130,255), width=1);

  L = len(ExtT)
  for i in range(L-1):
   draw.line([i*sizeX/L, ty(ExtT[i]), (i+1)*sizeX/L, ty(ExtT[i+1])], fill=lineColor, width=1)

  for i in range(L):
   draw.point([i*sizeX/L, ty(FridgeT[i])], fill=(255,200,255));

  for i in range(L):
   draw.point([i*sizeX/L, ry(RoomT[i])], fill=lineIColor);

  r = 3
  draw.ellipse([curPos-r, ry(LastRoomT)-r, curPos+r, ry(LastRoomT)+r], outline=lineIColor)

  for i in range(L):
   draw.point([i*sizeX/L, sizeY/2-ty((BaroP[i]-760-10))], fill=(224,86,27));

  draw.text([2,2], "%02d:%02d:%02d"%(tim.hour, tim.minute, tim.second), fill=strobeColor);


#
# Main WSGI application handler
#
def application(environ, start_response):
 global comC, comR, LockC, LockR, LockCfg, RoomT, FridgeT, BaroP, ClimateHist, FanHist

 def GenImg():
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
  global CfgAcCtlEnabled, CfgEvents, LockCfg, CfgFanOnTime, CfgFanOffTime, MinutesToFanOn

  # Parse configuration parameters, if any

  params = parse_qs(environ.get('QUERY_STRING', ''))

  if 'Add' in params:		# Add new event
    with LockCfg:
      CfgEvents.append(SchedEvent())

  elif 'Del' in params:		# Delete requested
    i = int(params['Del'][0])
    with LockCfg:
      del CfgEvents[i]

  elif 'Save' in params:	# Save requested
    with LockCfg:
      if 'ControlAC' in params:
        CfgAcCtlEnabled = True
      else:
        CfgAcCtlEnabled = False

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
        if m:			# Match -- "indexed" name, must belong to scheduler event
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
  r.append("<td>"+GenerateCurrentFanOnOffHtmlString()+"</td></tr>")
  r.append("""<table cellpadding=2 cellspacing=2 border=1>""")
  r.append("""<tr>Timed events</tr>""")
  for i in range(len(CfgEvents)):
    r.append(CfgEvents[i].GenerateFormString(i))
  r.append("""</table>""")
  r.append("""<INPUT TYPE=submit NAME="Add" value="Add">""")
  r.append("""<INPUT TYPE=submit NAME="Save" value="Save">""")
  r.append("""</form>""")
  return r


 def CondCtl():
  global comC, comR, LockCfg, CfgAcCtlEnabled, TargetTemp, CfgFanOnTime, CfgFanOffTime

  def TargetTInc(com, cmd):
   global CfgAcCtlEnabled, TargetTemp
   t = GetRoomTargetT()
   t = t + 0.5
   TargetTemp = t
   WaitReply(com, 'AT*TGN=%d'%(t*10))
   if CfgAcCtlEnabled:
      WaitReply(comR, 'ATAC=%d'%int(TargetTemp)) # Switch AC on, set temperature


  def TargetTDec(com, cmd):
   global CfgAcCtlEnabled, TargetTemp
   t = GetRoomTargetT()
   t = t - 0.5
   TargetTemp = t
   WaitReply(com, 'AT*TGN=%d'%(t*10))
   if CfgAcCtlEnabled:
      WaitReply(comR, 'ATAC=%d'%int(TargetTemp)) # Switch AC on, set temperature

  commandMap = [
   ('CondCtlMode', 'On',    MasterOn,   '',	   0),
   ('CondCtlMode', 'Off',   MasterOff,  '',	   0),
   ('TargetT',     '+',     TargetTInc, '',        comC),
   ('TargetT',     '-',     TargetTDec, '',        comC),
   ('HighFanMode', 'On',    FanCtl,     'AT*ENH=1', comC),
   ('HighFanMode', 'Off',   FanCtl,     'AT*ENH=0', comC),
   ('HighFanMode', 'Forced',FanCtl,     'AT*ENH=2', comC),
   ('LowFanMode',  'On',    WaitReply, 'AT*ENL=1', comC),
   ('LowFanMode',  'Off',   WaitReply, 'AT*ENL=0', comC),
   ('LowFanMode',  'Forced',WaitReply, 'AT*ENL=2', comC),
   ('BlindsMode',  '88',    WaitReply, 'ATSRV=88', comR),
   ('BlindsMode',  '90',    WaitReply, 'ATSRV=90', comR),
   ('BlindsMode',  '95',    WaitReply, 'ATSRV=95', comR),
   ('BlindsMode',  '100',   WaitReply, 'ATSRV=100',comR) ]


  # Do configuration if requested (if parameters are present in query string)
  params = parse_qs(environ.get('QUERY_STRING', ''))
  for t in commandMap:
   (name, value, fnc, cmd, port) = t
   if name in params:
    if value == escape(params[name][0]):
     fnc(port, cmd)

#  if 'LightMode' in form:
#   if form['LightMode'].value == 'Pulse':
#    WaitReply(comR, 'ATIRPM=2000');
#    WaitReply(comR, 'ATDLY=500');
#    WaitReply(comR, 'ATIRPM=200');

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

  r.append(MainFormHtmlTemplate%(GetCurrentMode(),
                                 rt, GetRoomTargetT(), int(TargetTemp), xt,
                                 GetCurrentHighFanMode(),
                                 GetCurrentBlindsMode(),
                                 recentRate))
  # Sync time with CondCtl
  t=time.localtime()
  WaitReply(comC, "AT*TMH=%d"%t.tm_hour)
  WaitReply(comC, "AT*TMM=%d"%t.tm_min)
  WaitReply(comC, "AT*TMS=%d"%t.tm_sec)
  WaitReply(comC, "AT*TMW=%d"%t.tm_wday)
  return r

########## Main web server entry

 path = environ.get('PATH_INFO', '').lstrip('/')
 r =["N/A"]

 if path == 'cgi-bin/genimg':
  start_response('200 OK', [('Content-type', 'image/png')])

  for i in range(3):
   try:
    r = GenImg()
   except PortError, x:
    Reconnect(x)
   else:
    break;
  return r

 elif path == 'cgi-bin/settings':
  start_response('200 OK', [('Content-Type','text/html; charset=ISO-8859-1')])
  r = SettingsPage()
  return r

 elif path == 'cgi-bin/condctl':
  start_response('200 OK', [('Content-Type','text/html; charset=ISO-8859-1')])
  for i in range(3):
   try:
    r = CondCtl()
   except PortError, x:
    Reconnect(x)
   else:
    break;
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


# ===========================================
# Service thread class
#  -- gathering room temperature statistics, scheduler, other service actions
# ===========================================
class ServiceThreadClass(threading.Thread):
 def run(self):
  global LockCfg, RoomT, PrevExtT, ClimateHist, ClimateOn, FanHist, FanOn, comC, MinutesToFanOn, CfgFanOnTime, CfgFanOffTime

  LogLine("Service thread started")

  recentDay = datetime.datetime.now().day
  recentSampling = datetime.datetime.now()
  recentTime = recentSampling

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
         LogLine("Forced")

     if minutesToFanOff != 0:
       minutesToFanOff -= 1
       if minutesToFanOff == 0:
         FanCtl(comC, 'AT*ENH=1')
         MinutesToFanOn = CfgFanOffTime
         LogLine("Norm")

    # Handle periodic sampling for graphs
    newSampling = datetime.datetime.now()
    if (newSampling - recentSampling).total_seconds() >= 60*2:
      recentSampling = newSampling

      # Sample parameters, store into the history arrays
      xt,rt,ft = GetTemperatures()
      p = GetBaroP()
      tim = datetime.datetime.now()
      ndx = (tim.hour * 60 + tim.minute + tim.second/60.)/2.
      ndx = int(round(ndx))
      RoomT[ndx] = rt
      FridgeT[ndx] = ft
      ExtT[ndx] = xt

      ClimateHist[ndx] = ClimateOn
      FanHist[ndx] = FanOn
      BaroP[ndx] = p

      expNdx = (lastNdx + 1) % len(RoomT)
      if expNdx != ndx:        # If we skipped one entry due to time slippage -- fill the skipped one
        RoomT[expNdx] = rt
        FridgeT[expNdx] = ft
        ExtT[expNdx] = xt
        ClimateHist[expNdx] = ClimateOn
        FanHist[expNdx] = FanOn
        BaroP[expNdx] = p

      lastNdx = ndx

      LogLine("Ext T: %f, Room T: %f, Baro P: %f"%(xt, rt, p))
      PrepImg()

      # After midnight, once: Dump external temperature for the last 24hrs, shift last days graphs
      if recentDay != datetime.datetime.now().day:
        recentDay = datetime.datetime.now().day

        PrevExtT[1] = PrevExtT[0]
        PrevExtT[0] = ExtT
        logT = open('/www/cgi-bin/ext_temp.log',"a+")
        strTemp = map(lambda(x): str(x)+' ', ExtT)
        logT.write("["+DateTime()+"] ")
        logT.writelines(strTemp[0:len(strTemp)])
        logT.write('\n')
        logT.close()

    time.sleep(60) # Sleep for 1 minute


# ===========================================
#  Main application, starting WSGI server
# ===========================================

# Open log file
LogHandle = open('/www/cgi-bin/condsrv.log','w')

LogLine("*** CondSrv home CondCtl and other peripherals WSGI server ***")

# Create lock objects for accessing comC, comR and configuration state
LockC = threading.Lock()
LockR = threading.Lock()
LockCfg = threading.Lock()
LockImg = threading.Lock()

# Create lists for various measurements
ExtT = [0. for i in range(60*24/2+1)]
RoomT = [0. for i in range(60*24/2+1)]
FridgeT = [0. for i in range(60*24/2+1)]
BaroP = [0. for i in range(60*24/2+1)]
ClimateHist   = [False for i in range(60*24/2+1)]
FanHist = [False for i in range(60*24/2+1)]
PrevExtT = [[0. for i in range(60*24/2+1)] for j in range(2)]

# Read global configuration variables from the database
LogLine("Loading configuration from condsrv")
sh = shelve.open('condsrv')
if 'CfgAcCtlEnabled' in sh:
  CfgAcCtlEnabled = sh['CfgAcCtlEnabled']
  LogLine(" global state loaded")
else:
  CfgAcCtlEnabled = False
  LogLine(" no global state loaded")

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
#LogLine("Loading previous day temperatures")
#f = open('/www/cgi-bin/ext_temp.log',"r")
#lineList = f.readlines()
#f.close()
#if len(lineList) >= 1:
#  s = lineList[len(lineList)-1]
#t = re.search('\[.*\] (.*) $', s)
#if t:
#  t = t.group(1).split(' ')
#  PrevExtT[0] = map(lambda(x): int(x), t)
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

WaitReply(comC, "AT*ECB=200")

# Read current target temperature and mode from CondCtl device, sync state
GetRoomTargetT()
LogLine("Target temperature read: %4.1f (AC: %d)"%(TargetTemp, int(TargetTemp)))

if GetCurrentMode() == 'On':
  MasterOn(0, 0)
  LogLine("CondCtl On, setting Master On")
else:
  MasterOff(0, 0)
  LogLine("CondCtl Off, setting Master Off")

FanOn = GetCurrentHighFanMode() == 'Forced'

#pdb.set_trace()

#
# Start service thread for internal room statistics and aux control
#
ServiceThread = ServiceThreadClass()
ServiceThread.start()

#
# Create and start web server
#
srv = make_server('10.0.0.126', 80, application)
LogLine("Starting web server...")

srv.serve_forever()

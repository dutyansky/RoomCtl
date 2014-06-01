//
// RoomCtl.ino -- Arduino sketch for miscellaneous low-level interfaceing for apartment 
//  equipment: Servos for blinds, IR control for AC unit, etc.
//
// See also condsrv.py for high-level control & web interface.
//
// Private project
//

#include <stdarg.h>
#include <Servo.h>
#include <Wire.h>

#include <Adafruit_BMP085.h>

#define DefaultServoPos 90


#define CMD_BUF_SIZE   255      // Command buffer size
#define MAX_CMD_SIZE   20       // Max command name

#define MAX_PULSES 60           // MAx number of pulses


// Digital pin #2: In: IR detector
// See http://learn.adafruit.com/ir-sensor/using-an-ir-sensor
#define IRpin           2
#define IRpin_PIN       PIND


#define Servo1Pin       3       // Digital pin #3: Out: Servo1 control
#define RelayPin        4
#define Servo2Pin       5

// Digital pin #12: Out: IR LED control
#define IRLedPin        12

// Digital pin #13: Used for onboard LED (default)


#define RESOLUTION      20                      // usec, sampling resolution
#define MAXPULSE        (60000/RESOLUTION)      // Max pulse length, usec


typedef int (*TCmdFnc)(const char *args);       // Command function type

//
// Command definition
//
typedef struct _TCmdDef {
  char  *Name;
  char  *Help;
  TCmdFnc CmdFnc;
} TCmdDef;

static unsigned char CmdBuf[CMD_BUF_SIZE];      // Command buffer
static unsigned char CmdBufNdx;                 // Command buffer pointer
static unsigned char CmdBufNdxSaved;            // Saved command buffer pointer
static unsigned char PrevChar;                  // Previous character (for AT prefix recognition)

#define IRRXBUF_SIZE    30                      // Size of IR RX buf, bytes
uint16_t IrRxBuf[IRRXBUF_SIZE];
uint8_t  IrRxBufNdx = 0;                          // Index for pulses we're storing


Servo Servo1;                                   // Servo control object
Servo Servo2;                                   // Servo control object
uint8_t Servo1Pos;

Adafruit_BMP085 Baro;                           // BMP085 baro sensor control object (learn.adafruit.com)


//
// ErrDiag - Output fatal diagnostics code through blinking, looping forever
//
void ErrDiag(int err) {
 int i;

 pinMode(13, OUTPUT);
 while(1) {
  for(i = 0; i < err; i++) {
   digitalWrite(13, HIGH);   // set the LED on
   delay(300);
   digitalWrite(13, LOW);    // set the LED off
   delay(200);
  }
  delay(1000);
 }
}

//
// PrintF - Printf to serial port
//
void PrintF(const char *fmt, ... ) {
 char tmp[128]; // resulting string limited to 128 chars

 va_list args;
 va_start (args, fmt );
 vsnprintf(tmp, 128, fmt, args);
 va_end (args);
 Serial.print(tmp);
}

//
// Initialize command line interpreter
//
void CmdInit(void) {

  CmdBufNdx = 255;              // Set "command not started" state
  CmdBufNdxSaved = 255;         // ...
  PrevChar = 0;                 // Invalidate previous character

  Serial.begin(9600);
  Serial.flush();
}


void PutRxByte(uint8_t b) {

 if(IrRxBufNdx >= IRRXBUF_SIZE) {
  PrintF("IR RX buffer overflow \r\n");
  return;
 }
 IrRxBuf[IrRxBufNdx++] = b;
}

//
// count_b - Internal function to count 0 bits in a byte
//
// Inputs:
//
//      b - Byte to count bits in
//
// Outputs:
//
//      (returns) - Number of 0-bits in the byte
//
int count_b(char b) {
 int i;
 int n = 0;

 for(i = 0; i < 8; i++) {
  if((b & 0x80) == 0)
    n++;
  b <<= 1;
 }
 return n;
}


//
// ComputeCheckSum() - Compute checksum for Samsung AC
//
// Inputs:
//
//      pkt - Packet buffer
//      sz - Size of packet, bytes
//
// Outputs:
//
//      None. pkt[1] is updated with checksum
//
void ComputeCheckSum(char *pkt, int sz) {
 int i, b;

 b = count_b(pkt[0]);
 for(i = 2; i < sz; i++)
   b += count_b(pkt[i]);

 pkt[1] = ((b % 15) << 4) + 2;
}


//
// Command functions
//
// For all:
// Inputs:
//
//      s - (ASCIIZ) - Pointer to arguments
//
// Outputs:
//
//      (returns) - 1: Success
//                  0: Error
//

//
// CmdAC() - Issue Samsung AC Air Conditioner IR command
// ATAC=22 -- On, set T=22
// ATAC=0  -- Off
//
//

//                   0     1     2     3     4     5     6       7     8     9    10    11    12    13
char irData[] =  { 0x02, 0x92, 0x0F, 0x00, 0x00, 0x00, 0xF0,   0x01, 0xE2, 0xFE, 0x71, 0x80, 0x11, 0xF0 };
char offData[] = { 0x02, 0xB2, 0x0F, 0x00, 0x00, 0x00, 0xC0,   0x01, 0xD2, 0x0F, 0x00, 0x00, 0x00, 0x00,
                                                               0x01, 0xF2, 0xFE, 0x71, 0xA0, 0x11, 0xC0 };

int CmdAC(const char *s) {
 int i, j, k;
 unsigned char b;       // Working byte variable
 int r;
 int t;                 // Target temperature or 0
 char *pkt;             // Packet to transit

 // Parse command arguments
 s++;   // Skip delimiter/=
 r = sscanf(s, "%u", &t);
 if(r != 1)
   return 0;

 if(t == 0) {   // Switching AC off
   pkt = offData;
 }
 else {         // Normal temperature, [17..32] degress Celsius
   pkt = irData;
   if(t < 17)
     t = 17;
   else
     if(t > 31)
       t = 31;

   irData[11] = (t - 16) << 4;

  // Compute packet checksums
   ComputeCheckSum(irData, 7);    // For subpacket #1
   ComputeCheckSum(irData+7, 7);  // For subpacket #2
 }


//
// Transmit the packet through IR
//
 cli();
 PulseIR(640);                  // First narrow pulse
 delayMicroseconds(6000);       // First long "off" time
 delayMicroseconds(6000);       // ...
 delayMicroseconds(5800);       // ...

// for(k = 0; k < 2; k++) {
 PulseIR(3000);                 // Long pre-start pulse
 delayMicroseconds(9000-60);    // Start "off" time

// Transmit data, low bits in byte first
// 500us per pulse
//  0: on + off
//  1: on + 3*off

 for(i = 0; i < 7; i++) {
   b = pkt[i];
   for(j = 0; j < 8; j++) {
    PulseIR(520);
    if((b & 1) == 0)
      delayMicroseconds(460);
    else
      delayMicroseconds(1480);
    b >>= 1;
   }
 }

 PulseIR(520);  // Final pulse

 delayMicroseconds(3000);       //

 PulseIR(3000);                 // Long pre-start pulse
 delayMicroseconds(9000);       // Start "off" time

 for(i = 0; i < 7; i++) {
   b = pkt[i+7];
   for(j = 0; j < 8; j++) {
    PulseIR(520);
    if((b & 1) == 0)
      delayMicroseconds(460);
    else
      delayMicroseconds(1480);
    b >>= 1;
   }
 }
 PulseIR(520);  // Final pulse

 delayMicroseconds(3000);

 if(pkt == offData) {           // Switching off, 3 packets to transmit
   PulseIR(3000);               // Long pre-start pulse
   delayMicroseconds(9000);     // Start "off" time

   for(i = 0; i < 7; i++) {
     b = pkt[i+14];
     for(j = 0; j < 8; j++) {
      PulseIR(520);
      if((b & 1) == 0)
        delayMicroseconds(460);
      else
        delayMicroseconds(1480);
      b >>= 1;
     }
   }
   PulseIR(520);  // Final pulse

   delayMicroseconds(3000);       //
 }

 sei();
 return 1;      // Signal OK
}


//
// CmdIRRD - Wait for and Read IR packet
//
int CmdIRRD(const char *s) {
 unsigned long t0, t1;
 int exitFlag = 0;
 uint16_t pulseLen;
 uint8_t i;
 uint8_t bitBuf, bitCnt;
 uint8_t bit;

 uint8_t pulseNdx = 0;

 IrRxBufNdx = 0;
 for(i = 0; i < IRRXBUF_SIZE; i++)
  IrRxBuf[i] = 0;
 bitCnt = 0;

 PrintF("IR input activated...\r\n");

 // Wait till the first low transition
 while ((IRpin_PIN & (1 << IRpin)) != 0) {
 }

 pulseLen = 0;
 t0 = micros();

 while ((IRpin_PIN & (1 << IRpin)) == 0) {  // pin is still low
   pulseLen++;
   delayMicroseconds(RESOLUTION);

   if(pulseLen >= MAXPULSE) { // Timeout
     PrintF("Initial pulse detection low state timeout\r\n");
     goto PktOut;
   }
 }

 // First low->high transition registered
 t1 = micros();
 if(t1-t0 > 800) {
  PrintF("Initial neg sync > 800us, aborting\r\n");
  return 1;
 }

 // 1. Wait for initial high long sync, 18000us
 t0 = t1;
 pulseLen = 0;
 while ((IRpin_PIN & (1 << IRpin)) != 0) {  // pin is still high
   pulseLen++;
   delayMicroseconds(RESOLUTION);
   if(pulseLen >= MAXPULSE) { // Timeout
     PrintF("Initial high long sync high state timeout\r\n");
     goto PktOut;
   }
 }

 // Initial high long sync registered
 t1 = micros();
 if(t1 - t0 < 17400-400 || t1 - t0 > 19000) {
  PrintF("Invalid initial long sync length: %ld\r\n", t1-t0);
  return 1;
 }

 // We also come here upon too long high state in bit Rx
 // 2. Wait for low->high, low pre-start pulse completion, 3000us
NextPkt:
 PutRxByte(0x77);
 bitBuf = 0;
 bitCnt = 0;

 t0 = t1;
 pulseLen = 0;
 while ((IRpin_PIN & (1 << IRpin)) == 0) {  // pin is still low
   pulseLen++;
   delayMicroseconds(RESOLUTION);

   if(pulseLen >= MAXPULSE) { // Timeout
     PrintF("Pre-start pulse detection low state timeout\r\n");
     goto PktOut;
   }
 }
 t1 = micros();

 // Initial low pre-start pulse registered
 if(t1 - t0 < 2800 || t1 - t0 > 3400) {
  PrintF("Invalid initial low mid pulse length\r\n");
  return 1;
 }

 // 3. Wait for high start pulse, 9000us
 t0 = t1;
 pulseLen = 0;
 while ((IRpin_PIN & (1 << IRpin)) != 0) {  // pin is still high
   pulseLen++;
   delayMicroseconds(RESOLUTION);

   if(pulseLen >= MAXPULSE) { // Timeout
     PrintF("Start pulse, high state timeout\r\n");
     goto PktOut;
   }
 }
 t1 = micros();

 // Initial high start pulse registered
 if(t1 - t0 < 8600-100 || t1 - t0 > 8900+300) {
  PrintF("Invalid initial high start pulse length: %d\r\n", t1-t0);
  return 1;
 }

 // Leading (low) edge of bit pulse has been received (in t1)
 // 500us per pulse?
 // 0: low + high
 // 1: low + 3*high
 // 01000000 01000001 111100000000000000000000...
 // 10000000 01001011 0111...

 do {
  t0 = t1;                                      // Leading edge time
  pulseLen = 0;
  while ((IRpin_PIN & (1 << IRpin)) == 0) {     // pin is still low
   pulseLen++;
   delayMicroseconds(RESOLUTION);
   if(pulseLen >= MAXPULSE) {                   // Timeout
     PrintF("Bit pulse, low state timeout\r\n");
     goto PktOut;
   }
  }
  t1 = micros();                                // Low->High transition
  if(t1 - t0 < 500-100 || t1 - t0 > 500+300) {
   PrintF("Invalid low bit pulse length: %ld, at %d:%d\r\n", t1-t0, IrRxBufNdx, bitCnt);
   goto PktOut;
  }
  t0 = t1;
  pulseLen = 0;
  while ((IRpin_PIN & (1 << IRpin)) != 0) {     // pin is still high
   pulseLen++;
   delayMicroseconds(RESOLUTION);
   if(pulseLen >= MAXPULSE) {                    // Timeout
     PrintF("Bit pulse, high state timeout\r\n");
     goto PktOut;
   }
  }
  t1 = micros();
  pulseLen = t1-t0;
  if(pulseLen < 500-400) { //???
   PrintF("Too short high bit pulse length: %ld, at %d:%d\r\n", t1-t0, IrRxBufNdx, bitCnt);
   goto PktOut;
  }
  else
   if(pulseLen > 500*3+100)
    goto NextPkt;
   else
    if(pulseLen > 500+100)
      bit = 0x80;
    else
      bit = 0;
  bitBuf >>= 1;
  bitBuf |= bit;
  bitCnt++;
  if(bitCnt == 8) {
   PutRxByte(bitBuf);
   bitBuf = 0;
   bitCnt = 0;
  }

 } while(1);

PktOut:
 PrintF("Last bitCnt: %d, bytes received: %d\r\n", bitCnt, IrRxBufNdx);
 for(i = 0; i < IrRxBufNdx; i++) {
  PrintF("%02X ", IrRxBuf[i]);
 }
 PrintF("\r\n");

 return 1;

}

/*

Packet format:
(see also Ken Shiriff's articles on web)

"77" below are artificial packet sync/delimiters

cool, fan2, 24:  77 02 92 0F 00 00 00 F0 77 01 D2 FE 71 80 19 F0
                    Const
                       Chk: High nibble = sum of 0 bits in packet mod 15, low nibble = 2
                          Const
                             Const
                                Const
                                   00=Normal, 20=Quiet
                                      F0-normal, C0=Off-packet
                                            Const
                                               Chk: High nibble = sum of 0 bits in packet mod 15, low nibble = 2
                                                  FE=Normal, AF=???BlackBlow, AE=blinds_On,FE=blinds_off
                                                     71=Normal, 77=Turbo, 7F=SmartSaver, F1=AutoClean_toggle
                                                        Temp, high nibble=t-16, low=0, 2 if autoclean_toggle
                                                           Mode&Fan: High nibble: auto/cool/dry/fan/heat=0/1/2/3/4.
                                                                     Low Nibble: low/mid/high=5/9/B,
                                                                                 auto=1 (not available in Fan, always in Turbo, Quiet)
                                                                                 0D - always in auto
                                                                Cool: 15=low, 19=mid, 1B=high, 11=auto;
                                                                21=DryAuto;
                                                                35/39/3B=FanMode1/2/3;
                                                                45/49/4B, 41=HeatMode

                       xx                            powersave
                    [--C --------------]    [--C  ---   T   F  ]
cool, fan2, 24:  77 02 92 0F 00 00 00 F0 77 01 D2 FE 71 80 19 F0 --
cool, fan2, 25:  77 02 92 0F 00 00 00 F0 77 01 C2 FE 71 90 19 F0
cool, fan3, 25:  77 02 92 0F 00 00 00 F0 77 01 B2 FE 71 90 1B F0
cool, fan3, 24:  77 02 92 0F 00 00 00 F0 77 01 C2 FE 71 80 1B F0
cool, fan1, 24:  77 02 92 0F 00 00 00 F0 77 01 D2 FE 71 80 15 F0
                 77 02 92 0F 00 00 00 F0 77 01 D2 FE 71 80 15 F0
cool, fanA, 24:  77 02 92 0F 00 00 00 F0 77 01 E2 FE 71 80 11 F0
cool, fanA, 23:  77 02 92 0F 00 00 00 F0 77 01 C2 FE 71 70 11 F0
cool, fanA, 22:  77 02 92 0F 00 00 00 F0 77 01 D2 FE 71 60 11 F0 e.g., for the 2nd subpacket: # of 0 bits: 7 1 4 6 6 4 == 28 mod 15 = D
cool, fanA, 21:  77 02 92 0F 00 00 00 F0 77 01 D2 FE 71 50 11 F0
 quiet:          77 02 82 0F 00 00 20 F0 77 01 02 AF 71 80 11 F0
26...off:        77 02 B2 0F 00 00 00 C0 77 01 D2 0F 00 00 00 00
                                         77 01 F2 FE 71 A0 11 C0
26,...on:        77 02 92 0F 00 00 00 F0 77 01 D2 0F 00 00 00 00
                                         77 01 D2 FE 71 A0 11 F0
turbo:           77 02 92 0F 00 00 00 F0 77 01 B2 FE 77 90 11 F0
dry:             77 02 92 0F 00 00 00 F0 77 01 C2 FE 71 70 21 F0
fan:             77 02 92 0F 00 00 00 F0 77 01 C2 FE 71 80 35 F0
heat:            77 02 92 0F 00 00 00 F0 77 01 B2 FE 71 70 45 F0
auto:            77 02 92 0F 00 00 00 F0 77 01 D2 FE 71 80 0D F0
autoclean:       77 02 92 0F 00 00 00 F0 77 01 A2 FE F1 92 15 F0



*/


void PulseIR(long microsecs) {

// cli();
 while(microsecs > 0) {
  digitalWrite(IRLedPin, HIGH);  // this takes about 3 microseconds to happen
  delayMicroseconds(10-1);       // hang out for 10 microseconds
  digitalWrite(IRLedPin, LOW);   // this also takes about 3 microseconds
  delayMicroseconds(10-1);         // hang out for 10 microseconds
  microsecs -= 26;               // 26 us == 1/38kHz, carrier period completed, continue generation if time have not elapsed
 }
// sei();
}

//
// CmdIRPM - Pulse IR for given ms
// ATIRPM=20
//
int CmdIRPM(const char *s) {
 unsigned long t0, te, t1;
 int r, expired;   // "Expired" flag
 unsigned t;


 t0 = millis(); // Current time, ms

 s++;   // Skip delimiter/=
 r = sscanf(s, "%u", &t);
 if(r != 1)
  return 0;

 te = t0 + t;

 do {
  PulseIR(2400);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(1000);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(1000);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);
  PulseIR(600);
  delayMicroseconds(600);

  delay(22);

  t1 = millis();
  if(te >= t0) { // Normal case, no wraparound
   expired = (t1 >= te);
  }
  else {         // te is after wrap-around
   if(t1 > t0)   // No wrap around yet
    expired = 0;
   else
    expired = (t1 >= te);
  }

 } while(!expired);

 return 1;
}

//
// CmdDly - Delay for given ms
// ATIRPM=20
//
int CmdDly(const char *s) {
 unsigned t;
 int r;

 s++;   // Skip delimiter/=
 r = sscanf(s, "%u", &t);
 if(r != 1)
  return 0;

 delay(t);

 return 1;
}


int CmdSrvR(const char *s) {
  PrintF("Current position: %u\r\n",Servo1Pos);
  return 1;
}


int CmdBaroR(const char *s) {
 uint32_t p;

 p = Baro.readPressure();

 PrintF("Baro P:%lu\r\n", p);
 return 1;
}


//
// CmdSrv - Set servo position to NN
// ATSRV=20
//
int CmdSrv(const char *s) {
 unsigned t;
 unsigned t1;
 int r;

 s++;   // Skip delimiter/=
 r = sscanf(s, "%u", &t);
 if(r != 1)
  return 0;

 if(t < 78 || t > 100) {
  PrintF("Valid range: 78..100\r\n");
  return 0;
 }

Servo1Pos = 90;
 t1 = (t < Servo1Pos)? t - 3: t + 3;


 digitalWrite(RelayPin, HIGH);


 Servo1.write(t1+3);
 Servo2.write(180-t1-2);

 delay(3000);    // Wait
 Servo1.write(t+3);
 Servo2.write(180-t-2);
 delay(1500);    // Wait
 digitalWrite(RelayPin, LOW);

 Servo1Pos = t;

 return 1;
}


//
// Commands dispatch table
//
const TCmdDef CmdTbl[] = {
 { "IRRD", "Read IR packet from pin 2", CmdIRRD },
 { "IRPM", "Pulse IR packet for NN ms, ATIRPM=NN", CmdIRPM },
 { "DLY",  "Delay for NN ms, ATDLY=NN", CmdDly },
 { "SRV",  "Servo position 0..180, ATSRV=NN", CmdSrv },
 { "SRVR", "Servo position read", CmdSrvR },
 { "BARO", "Barometer data read", CmdBaroR },
 { "AC",   "AC IR packet", CmdAC },
 { 0, 0, 0}
};


//
// CmdExecute - Find & execute command
//
// Inputs:
//
//      CmdBuf - Command & arguments (without AT prefix)
//
// Outputs:
//
//      (returns) - 1: OK
//                  0: Error
//
int CmdExecute(void) {
 int i;
 char cmdName[MAX_CMD_SIZE];    // Command name, uppercased
 int argNdx;                    // Index of arguments
 unsigned char c;

//
// Isolate command name
//
 i = 0;
 argNdx = 0;
 while(i <= MAX_CMD_SIZE-1 && argNdx <= CmdBufNdx) {
  c = toupper(CmdBuf[argNdx++]);
  cmdName[i++] = c;
    // End of command body (delimiter, EOL, or $ after non-null command) -- break
  if(c == ' ' || c == '=' || c == '\0' || i > 1 && c == '$')
   break;
 }
 if(i > 0)
  cmdName[i-1] = '\0';
 else
  cmdName[0] = '\0';

 if(argNdx > 0)
  argNdx--;

 if(cmdName[0] == '\0')
  return 1;

 if(cmdName[0] == '$') {        // Global help request
  i = 0;
  while(CmdTbl[i].Name) {
   Serial.print(CmdTbl[i].Name);
   Serial.print(": ");
   Serial.println(CmdTbl[i].Help);
   i++;
  }
  return 1;
 }

//
// Find the corresponding command definition table entry
//
 i = 0;
 while(CmdTbl[i].Name) {
  if(!strcmp(CmdTbl[i].Name, cmdName)) {
   break;
  }
  i++;
 }


 if(CmdTbl[i].Name) {                   // If command definition found
  if(CmdBuf[argNdx] == '$') {           // Help request
   Serial.print(CmdTbl[i].Name);
   Serial.print(": ");
   Serial.println(CmdTbl[i].Help);      //  -- print it
   return 1;
  }
  return CmdTbl[i].CmdFnc((const char*)&CmdBuf[argNdx]); // Normal command processing
 }

 return 0;
}


//
// CmdService - Service routine for command line handling
// MUST be called in the main loop
//
void CmdService(void) {
  char c;

  while(Serial.available()) {
   c = Serial.read();
   Serial.write(c);
   if(toupper(c) == 'T' && toupper(PrevChar) == 'A') {
    CmdBufNdx = 0;
    PrevChar = 0;
   }
   else
    if(c == '/' && toupper(PrevChar) == 'A') { // A/ -- Repeat previous command
     CmdBufNdx = CmdBufNdxSaved;
     if(CmdExecute())
      Serial.println("OK");
     else
      Serial.println("ERROR");
      CmdBufNdx = 255;                   // Set "AT not started" mode
      PrevChar = 0;                      // Reset prev character
    }
    else
     if(c == '\r' && CmdBufNdx != 255) { // CR detected & in command accumulation mode
      CmdBufNdxSaved = CmdBufNdx;
      Serial.write('\n');                // Add LF
      CmdBuf[CmdBufNdx] = '\0';          // Set ASCIIZ EOL
      if(CmdExecute())
       Serial.println("OK");
      else
       Serial.println("ERROR");
      CmdBufNdx = 255;                   // Set "AT not started" mode
      PrevChar = 0;                      // Reset prev character
     }
     else
      if(CmdBufNdx != 255 && CmdBufNdx < CMD_BUF_SIZE) {
       CmdBuf[CmdBufNdx++] = c;
       PrevChar = c;
      }
      else
       PrevChar = c;
  }
}


//
// Setup routine
//
void setup() {

 Serial.begin(9600);
 Baro.begin(BMP085_ULTRALOWPOWER);
 pinMode(13, OUTPUT);           // Default onboard LED
 pinMode(IRLedPin, OUTPUT);     // IR LED pin
 pinMode(IRpin, INPUT);         // IR detector input
 pinMode(Servo1Pin, OUTPUT);    // Servo control
 pinMode(Servo2Pin, OUTPUT);    // Servo control
 pinMode(RelayPin, OUTPUT);     // Relay control
 digitalWrite(RelayPin, LOW);

 Servo1.attach(Servo1Pin);       // Attach servo
 Servo2.attach(Servo2Pin);       // Attach servo

 Servo1Pos = DefaultServoPos;
 Servo1.write(Servo1Pos);
 Servo2.write(180-Servo1Pos);

 CmdInit();                     // Initialize command line subsystem
}

//
// Main loop routine
//
void loop() {

  CmdService();             // Service command interpreter
}



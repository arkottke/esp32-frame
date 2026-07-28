

#ifndef __PINDEFINE_H__
#define __PINDEFINE_H__

//==============  Standard SPI Setting   ==============//
//Please modify the pin number
#define SPI_CS0		18
#define SPI_CS1		17
#define SPI_CLK		9
#define SPI_Data0	41
#define SPI_Data1	40
#define SPI_Data2	39
#define SPI_Data3	38

//==============   GPIO Setting   ==============//
//Please modify the pin number
#define EPD_BUSY	7   // Please set it as input pin
#define EPD_RST		6   // Please set it as output pin
#define LOAD_SW		45  // Please set it as output pin
#define SW2		12  // Manual image refresh (active-HIGH, internal pulldown, wakes from deep sleep)
#define SW4		14  // Factory reset button (active-HIGH, internal pulldown)

//===============================================

#define GPIO_LOW	0
#define GPIO_HIGH	1

//==============  State-of-health  ==============//
// Set BATT_ADC_CHANNEL to the ADC1 channel number wired to your voltage
// divider (e.g. ADC_CHANNEL_0 for GPIO1). Leave as -1 to disable.
#define BATT_ADC_CHANNEL  -1

#define FW_VERSION  "1.0.3"

#endif //#ifndef __PINDEFINE_H__

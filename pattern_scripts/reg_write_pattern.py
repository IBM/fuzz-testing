import os
import sys
import re
import time

INC = int('0x04', 16)
# REG_OFF = hex(int('0x00', 16))
file_name = ''

# Determining Device ID based on command line input with a default DEVICE_ID of 04:00.0
if (len(sys.argv)) != 2:
    DEVICE_ID = '01:00.0'
else:
    DEVICE_ID = str(sys.argv[1])

def main():
    # REG_OFF = hex(int('0x00', 16))
    # output_file = open(file_name, 'w')
    register = []
    value = []

    read_file(register,value)
    config(register,value)

def config(register,value):
    #REG_OFF = hex(int('0x00', 16))

    for i in range(1, 1834):
        #output.append(os.popen('sudo setpci -v -s ' + DEVICE_ID + ' ' + str(REG_OFF) + '.L=').read())
        oper = os.popen('setpci -v -s' + DEVICE_ID + ' ' + str(register[i-1]) + '.B=' + value[i-1]).read()
        print(oper)
        # delay in seconds
        time.sleep(5)
        # change REG_OFF and INC from string to hex and update REG_OFF
        #REG_OFF_int = int(REG_OFF, 16)
        #REG_OFF = hex(REG_OFF_int + INC)


def read_file(register,value):
    file_name = 'pattern_file'
    
    output_file = open(file_name, 'r')
    # Read list from a file
    for line in output_file:
        pattern=line.replace('@',' ').split()
        register.append(pattern[1])
        value.append(pattern[2])
    #print(register)
    #print(value)
    output_file.close()

# Start script
if __name__ == "__main__":
    main()

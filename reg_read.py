import os
import sys

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
    output = []

    config(output)
    write_file(output)

def config(output):
    REG_OFF = hex(int('0x00', 16))

    for i in range(1, 64):
        output.append(os.popen('sudo setpci -v -s ' + DEVICE_ID + ' ' + str(REG_OFF) + '.L').read())
        
        # change REG_OFF and INC from string to hex and update REG_OFF
        REG_OFF_int = int(REG_OFF, 16)
        REG_OFF = hex(REG_OFF_int + INC)


def write_file(output):
    file_name = 'pci_read'
    
    output_file = open(file_name, 'w')
    # Print output list to a file
    output_file.writelines(output)    
    output_file.close()


# Start script
if __name__ == "__main__":
    main()

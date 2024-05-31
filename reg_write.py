import os
from sre_constants import REPEAT_ONE
import sys
import random
from random import randrange
import time


# initializing register incriment for serial writes and output file name
INC = int('0x01', 16)
file_name = 'pci_write'

################################################################
# Turns on proper enables to from user arguments.
# Determines which addresses in the PCIe configuration space 
# should not be written to.
# Logs time and date in a separte file.
# Writes to the device's PCIe configuration space and logs
# results in terminal and separate file.
################################################################
def main():
    # intialize settings for test, turn on proper enables
    settings()
    
    addr = []
    if cont == 1:
        addr = skip_reg()
    
    # Start file
    with open(file_name, "a") as myfile:
        date = os.popen('date').read()
        myfile.write('\n' + args + '\n')
        myfile.write(date)
        myfile.close()

    output = []
    repeat_func(output, addr)


###################################################################
# Enables set based on inputs including:
# choose device ID, range of config space (basic, full, extended), 
# new/continue, serial/random, registers to be skipped,
# number of repeat tests, and help section. 
###################################################################
def settings():
    # global device id input (-d=)
    global DEVICE_ID

    # global range input enables (-r=)
    global basic
    global full
    global extended

    # global new/continue enables (-n=)
    global new
    global cont

    # global order enables (-o=)
    global serial
    global rand

    # global skip register values (-s=)
    global skip_arg
    skip_arg = []

    # global value that repeats test a certain number of times (-i=)
    global repeat
    repeat = 1

    # global save argument values
    global args

    args = listToString(sys.argv)
    sys.argv.pop(0)
    for arg in sys.argv:
        arg_type = arg[0:3].lower()
        arg_input = arg[3:].lower()
        
        if arg_type == '-d=':
            DEVICE_ID = arg_input
        
        elif arg_type == '-r=':
            if arg_input == 'basic':
                basic = 64
                full = 0
                extended = 0
            elif arg_input == 'full':
                full = 256
                basic = 0
                extended = 0
            elif arg_input == 'extended':
                extended = 4096
                basic = 0
                full = 0
            else:
                print('Error: Invalid Range Input Entry (-r)')
        
        elif arg_type == '-n=':
            if arg_input == 'new':
                 new = 1
                 cont = 0
            elif arg_input == 'continue':
                cont = 1
                new = 0
            else:
                print('Error: Invalid Next Test Input (-n=)')
        
        elif arg_type == '-o=':
            if arg_input == 'serial':
                serial = 1
                rand = 0
            elif arg_input == 'random':
                rand = 1
                serial = 0
            else:
                print('Error: Invalid Order Input (-o=)')
        
        elif arg_type == '-s=':
            if ',' in arg_input:
                skip_arg = arg_input.lower().split(',')
            else:
                skip_arg = [arg_input.lower()]
            for each in skip_arg:
                i = skip_arg.index(each)
                each_int = int(each, 16)
                if basic != 0 and each_int < basic:
                    # replace hex value in list with its int value
                    skip_arg[i] = each_int
                elif full != 0 and each_int < full:
                    skip_arg[i] = each_int
                elif extended != 0 and each_int < extended:
                    skip_arg[i] = each_int
                else:
                    skip_arg.remove(each)
        
        elif arg_type == '-i=':
            if int(arg_input) > 0:
                repeat = int(arg_input)
            else:
                repeat = 1
            print('repeat: ' + str(repeat)) # DEBUG
        
        elif arg[0:2] == '-h':
            print('List of Commands:\n' +
                  '-d=: Device ID\n' +
                  '-r=: Range of config space (basic = 64 byes, full = 256 bytes, extended = 4096 bytes)\n' +
                  '-n=: Next test type (new = do not ignore failing registers, cont = ignore previously failed registers)\n' +
                  '-o=: Order registers are being tested (serial = test registers in corresponding order from 0, random = test registers randomly\n' +
                  '-s=: Registers to be skipped in the next program run. Provide hexadecimal register value(s) separated with a comma (\',\').\n' +
                  '-i=: Integer value for the number of times the test should repeat with the same parameters\n' +
                  '-h:  Help section with list of possible input commands.')
            sys.exit()
        
        else:
            print('Error: \'' + arg_type + '\' is not a valid input command')
            sys.exit()


###################################################################
# Run the program for as many iterations as user requested.
# Inputs:
#   output = list of results from running setpci at each address 
#            being written to.
#   addr = list of registers written to in the previous test.
###################################################################       
def repeat_func(output, addr):
    for each in range(repeat):
        config(output, addr)
    

###################################################################
# Chooses serial or random and runs setpci utility.
# Resulting writes are logged in the terminal and a separter file.
# Input: 
#   output = list of results from running setpci at each address 
#            being written to.
#   addr = list of registers written to in the previous test.
###################################################################
def config(output, addr):
    random_list = []
    REG_OFF = hex(int('0x00', 16))
    byte_range = 0
    start = True
    iter = 0

    byte_range = set_range(byte_range, addr)

    # runs serial test for config space writes
    if serial == 1:
        for each in range(byte_range):
            if each in skip_arg:
                REG_OFF_int = int(REG_OFF, 16)
                REG_OFF = hex(REG_OFF_int + INC)
                continue
            if cont == 1 and start == True:
                each = int(addr[0], 16) + 1
                REG_OFF_int = int(REG_OFF, 16)
                REG_OFF = hex(REG_OFF_int + each)
                start = False
            print('Iter: ' + str(iter))
            print(str(REG_OFF))
            RAND = os.popen('openssl rand -hex 1').read()
            value = os.popen('sudo setpci -v -s ' + DEVICE_ID + ' ' + str(REG_OFF) + '.B=' + str(RAND)).read()
            print(value)
            output.append(value)
            write_file(output)
            # delay in seconds
            time.sleep(0.5)
            # change REG_OFF and INC from string to hex and update REG_OFF
            REG_OFF_int = int(REG_OFF, 16)
            REG_OFF = hex(REG_OFF_int + INC)
            iter += 1
    
    # Runs random test for config space writes
    elif rand == 1:
        # creates a list of registers that will be chosen randdomly to write to
        for i in range(byte_range):
            if i in skip_arg:
                continue
            random_list.append(i)
        # remove the register that will be skipped from the list of registers.
        if cont == 1 and start == True:
            for each in addr:
                random_list.remove(int(each, 16))
            start = False
        for each in range(len(random_list)):
            print('Iter: ' + str(iter))
            REG = random.choice(random_list)
            random_list.remove(REG)
            REG = hex(REG)
            print(str(REG))
            RAND = os.popen('openssl rand -hex 1').read()
            value = os.popen('sudo setpci -vD -s ' + DEVICE_ID + ' ' + str(REG) + '.B=' + str(RAND)).read()
            print(value)
            output.append(value)
            write_file(output)
            # delay in seconds
            time.sleep(0.5)
            iter += 1


###################################################################
# Wrties the results from setpci to a separate file.
# Input: 
#   output = list of results from running setpci at each address 
#            being written to.
###################################################################
def write_file(output):
    with open(file_name, "a") as myfile:
        myfile.write(output[len(output)-1])
        myfile.close()


###################################################################
# Converts a list to a string.
# Input: 
#   s = a list
# Output: 
#   str1 = the input list as a string with ' ' splitting 
#          each element.
###################################################################
def listToString(s):
    str1 = "" 
    for ele in s: 
        str1 = str1 + ele + ' '  
    str1 = str1[:len(str1)-1]
    return str1


###################################################################
# Sets the range of registers that will be written to based on 
# user input for serial writes.
# Inputs:
#   byte_range = number of registers that will be written to.
#   addr = list of registers written to in the previous test.
# Output:
#   byte_range = the correct number of registers that will be
#                written to based on user inputs.
###################################################################
def set_range(byte_range, addr):
    if basic > 0:
        byte_range = basic
    elif full > 0:
        byte_range = full
    elif extended > 0:
        byte_range = extended
    if cont == 1 and serial == 1:
        if basic > 0:
            byte_range = basic - int(addr[0], 16) - 1
        elif full > 0:
            byte_range = full - int(addr[0], 16) - 1
        elif extended > 0:
            byte_range = extended - int(addr[0], 16) - 1
    return byte_range


###################################################################
# Creates to a list of registers that should be skipped for 
# continued tests.
# Output:
#   addr_list = list of addresses that should not be written to.
###################################################################
def skip_reg():
    addr_list = []
    for line in reversed(list(open(file_name))):
        last_line = line.rstrip()
        if '@' in last_line:
            last_line = last_line.split(' ')
            addr = hex(int(last_line[1][1:], 16))
            addr_list.append(addr)
        else:
            print('break')
            #f i > 0:
            break
            continue
    return addr_list


if __name__ == "__main__":
    main()
    

# fuzz-testing
Scripts to perform fuzz testing on the PCIe exposed space

## List of Commands:
To read a PCIe configuration space:

`python reg_read.py <DEVICE_ID>`

To write to a PCIe configuration space:

`python reg_write.py -d=<DEVICE_ID> -r=basic -n=new -o=serial`

Display help options:

`python reg_write.py -h`

-d=: Device ID
-r=: Range of config space (basic = 64 byes, full = 256 bytes, extended = 4096 bytes)
-n=: Next test type (new = do not ignore failing registers, cont = ignore previously failed registers)
-o=: Order registers are being tested (serial = test registers in corresponding order from 0, random = test registers randomly
-s=: Registers to be skipped in the next program run. Provide hexadecimal register value(s) separated with a comma (',')
-i=: Integer value for the number of times the test should repeat with the same parameters
-h:  Help section with list of possible input commands.

To rewrite a pattern to the configuration space, create a `pattern_file` and run:

`python pattern_scripts/reg_write_pattern.py <device_id>`

An example pattern file can be found in the `pattern_scripts` subfolder.

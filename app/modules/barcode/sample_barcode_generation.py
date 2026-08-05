from barcode import Code128
from barcode.writer import ImageWriter

barcode = Code128("EMP000123", writer=ImageWriter())
filename = barcode.save("employee_barcode")
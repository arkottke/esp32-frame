PORT      ?= /dev/ttyUSB0
ENV_FILE   := ../.env
NVS_GEN    := $(IDF_PATH)/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py
NVS_CSV    := build/nvs_provision.csv
NVS_BIN    := build/nvs_provision.bin
NVS_OFFSET := 0x9000
NVS_SIZE   := 0x6000

.PHONY: build flash monitor flash-monitor provision flash-all clean

build:
	idf.py build

flash:
	idf.py -p $(PORT) flash

monitor:
	idf.py -p $(PORT) monitor

flash-monitor:
	idf.py -p $(PORT) flash monitor

provision: $(ENV_FILE)
	python3 scripts/gen_nvs_csv.py $(ENV_FILE) $(NVS_CSV)
	python3 $(NVS_GEN) generate $(NVS_CSV) $(NVS_BIN) $(NVS_SIZE)
	esptool -p $(PORT) write-flash $(NVS_OFFSET) $(NVS_BIN)
	@echo "NVS provisioned."

flash-all: flash provision

clean:
	idf.py fullclean
	rm -f $(NVS_CSV) $(NVS_BIN)

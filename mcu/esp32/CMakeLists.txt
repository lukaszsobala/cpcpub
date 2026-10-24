# cpcpub for the Espressif ESP32 family, built with ESP-IDF 5.0 or newer.
#
#   . ~/esp-idf/export.sh
#   idf.py -C mcu/esp32 set-target esp32s3      # or esp32, esp32c3, esp32c6, ...
#   idf.py -C mcu/esp32 build flash
#
# See mcu/README.md.

cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(cpcpub)

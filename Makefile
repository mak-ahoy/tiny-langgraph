CC      ?= cc
PYTHON  ?= python3
BUILD   ?= build
EXTRA   ?=
CFLAGS   = -std=c11 -O2 -g -Wall -Wextra -Wpedantic -Wshadow -Werror -Icore -Ilm $(EXTRA)

CORE     = core/tlg.c core/tlg_plan.c core/tlg_trace.c core/tlg_hub.c core/tlg_agent.c core/tlg_count.c
HEADERS  = $(wildcard core/*.h lm/*.h)
DG       = examples/door_guard
DG_SRC   = $(DG)/main.c $(DG)/agent.c $(DG)/scenarios.c $(DG)/generated/door_guard_schema.c lm/tlg_lm.c
DG_INC   = -I$(DG) -I$(DG)/generated

.PHONY: all test examples demo trace asan py-test agents model-check check viewer-demo arduino fake-board clean

all: test examples

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/test_tlg: tests/test_tlg.c $(CORE) $(HEADERS) | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tests/test_tlg.c $(CORE)

$(BUILD)/test_plan: tests/test_plan.c $(CORE) $(HEADERS) | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tests/test_plan.c $(CORE)

$(BUILD)/test_hub: tests/test_hub.c $(CORE) $(HEADERS) | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tests/test_hub.c $(CORE)

$(BUILD)/hello: examples/hello/main.c $(CORE) $(HEADERS) | $(BUILD)
	$(CC) $(CFLAGS) -o $@ examples/hello/main.c $(CORE)

$(BUILD)/plant_care: examples/plant_care/main.c $(CORE) $(HEADERS) | $(BUILD)
	$(CC) $(CFLAGS) -o $@ examples/plant_care/main.c $(CORE)

$(BUILD)/door_guard: $(DG_SRC) $(CORE) $(HEADERS) $(wildcard $(DG)/*.h $(DG)/generated/*.h) | $(BUILD)
	$(CC) $(CFLAGS) $(DG_INC) -o $@ $(DG_SRC) $(CORE) -lm

test: $(BUILD)/test_tlg $(BUILD)/test_plan $(BUILD)/test_hub
	./$(BUILD)/test_tlg
	./$(BUILD)/test_plan
	./$(BUILD)/test_hub

examples: $(BUILD)/hello $(BUILD)/plant_care $(BUILD)/door_guard

demo: examples
	./$(BUILD)/hello
	./$(BUILD)/plant_care
	./$(BUILD)/door_guard

# Trace events for the viewer: open build/door_guard.log in viewer/index.html
trace: $(BUILD)/door_guard
	./$(BUILD)/door_guard --trace > $(BUILD)/door_guard.log
	@echo "wrote $(BUILD)/door_guard.log: open it in viewer/index.html"

# Toolchain tests, including the C-vs-Python cross-check on every situation of every
# example schema (needs PyYAML and a C compiler).
py-test:
	PYTHONPATH=python $(PYTHON) -m unittest discover -s tests -p "test_*.py"

# Python agents: convert to C, label every situation, check the C decides like the Python.
agents:
	PYTHONPATH=python $(PYTHON) examples/pet_watcher/agent.py check
	PYTHONPATH=python $(PYTHON) examples/porch_light/agent.py check
	cd examples/feeding_station && PYTHONPATH=../../python $(PYTHON) feeder.py check && PYTHONPATH=../../python $(PYTHON) watcher.py check

# Regenerate the data and check the committed model still answers like the rules.
model-check: $(BUILD)/door_guard
	$(PYTHON) tools/tlg.py data $(DG)/schema.yaml > /dev/null
	./$(BUILD)/door_guard --eval $(DG)/data/val.txt

check: test examples py-test agents model-check

viewer-demo: $(BUILD)/door_guard
	./$(BUILD)/door_guard --trace > $(BUILD)/demo.log
	$(PYTHON) tools/embed_demo.py $(BUILD)/demo.log viewer/index.html

# Ready-to-open Arduino sketch in build/arduino/door_guard_web (needs only Python)
arduino:
	$(PYTHON) tools/arduino_bundle.py

# The board's web API on your PC, for trying the viewer without hardware: http://127.0.0.1:8080
fake-board: trace
	$(PYTHON) tools/fake_board.py $(BUILD)/door_guard.log

asan:
	$(MAKE) BUILD=build-asan EXTRA="-fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all" test examples
	./build-asan/hello > /dev/null
	./build-asan/plant_care > /dev/null
	./build-asan/door_guard > /dev/null
	./build-asan/door_guard --corrupt 300 > /dev/null

clean:
	rm -rf build build-asan

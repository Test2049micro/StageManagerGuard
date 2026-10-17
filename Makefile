.PHONY: build test install uninstall clean

build:
	./build.sh

test: build
	python3 -u tests/test_stage_manager_guard.py
	python3 -u tests/test_adversarial.py
	python3 -u tests/test_fuzz.py 20261017
	python3 -u tests/test_fuzz.py 1
	python3 -u tests/test_fuzz.py 42
	python3 -u tests/test_chaos.py 20261017 10
	python3 -u tests/test_chaos.py 7 10

install:
	./install.sh

uninstall:
	./uninstall.sh

clean:
	rm -rf bin __pycache__ src/__pycache__

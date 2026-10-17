.PHONY: build test install uninstall clean

build:
	./build.sh

test: build
	python3 -u tests/test_stage_manager_guard.py
	python3 -u tests/test_adversarial.py

install:
	./install.sh

uninstall:
	./uninstall.sh

clean:
	rm -rf bin __pycache__ src/__pycache__

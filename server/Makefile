VENV   := .venv
PYTHON := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
HOST   := 0.0.0.0
PORT   := 8000

.PHONY: run bot install venv clean

run: $(VENV)
	$(VENV)/bin/uvicorn main:app --host $(HOST) --port $(PORT) --reload

bot: $(VENV)
	$(PYTHON) telegram_bot.py

install: $(VENV)

$(VENV): requirements.txt
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@touch $(VENV)

clean:
	rm -rf $(VENV) __pycache__ plugins/__pycache__ *.pyc

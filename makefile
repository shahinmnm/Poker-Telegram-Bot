all: install lint test run
run:
	python3 main.py
up:
	docker-compose --env-file .env up --build -d
logs:
	docker-compose logs bot
down:
	docker-compose down
debug:
	POKERBOT_DEBUG=1 python3 main.py
test:
	python3 -m unittest discover -s ./tests
lint:
	python3 -m flake8 .
install:
	pip3 install -r requirements.txt
.env:
ifeq ($(POKERBOT_TOKEN),)
        @printf "Usage:\n\n\tmake .env POKERBOT_TOKEN=<your telegram token> [CRITICAL_CHAT_ID=...] [OPERATIONAL_CHAT_ID=...] [DIGEST_CHAT_ID=...]\n\n"
        @exit 1
endif
        printf "POKERBOT_TOKEN=$(POKERBOT_TOKEN)\n" > .env
        printf "POKERBOT_REDIS_HOST=localhost\nPOKERBOT_REDIS_PORT=6379\nPOKERBOT_REDIS_PASS=\nPOKERBOT_REDIS_DB=0\n" >> .env
        printf "CRITICAL_CHAT_ID=$(CRITICAL_CHAT_ID)\n" >> .env
        printf "OPERATIONAL_CHAT_ID=$(OPERATIONAL_CHAT_ID)\n" >> .env
        printf "DIGEST_CHAT_ID=$(DIGEST_CHAT_ID)\n" >> .env

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
        @printf "Usage:\n\n\tmake .env POKERBOT_TOKEN=<your telegram token> ADMIN_CHAT_ID=<your telegram user id>\n\n"
        @exit 1
endif
ifeq ($(ADMIN_CHAT_ID),)
        @printf "ADMIN_CHAT_ID is required for the alert bridge.\n"
        @exit 1
endif
        printf "POKERBOT_TOKEN=$(POKERBOT_TOKEN)\n" > .env
        printf "POKERBOT_REDIS_HOST=localhost\nPOKERBOT_REDIS_PORT=6379\nPOKERBOT_REDIS_PASS=\nPOKERBOT_REDIS_DB=0\n" >> .env
        printf "# Admin's private Telegram chat ID\n" >> .env
        printf "ADMIN_CHAT_ID=$(ADMIN_CHAT_ID)\n" >> .env

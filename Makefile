.PHONY: help
help: ## show help message
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  \033[36m\033[0m\n"} /^[$$()% a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

.git/hooks/pre-commit: pre-commit
	cp pre-commit .git/hooks/

.PHONY: install-dev
install-dev: .git/hooks/pre-commit ## install dependencies for development
	poetry install

.PHONY: install
install: .git/hooks/pre-commit ## install dependencies
	poetry install --no-dev

.PHONY: format
format: ## format code
	poetry run isort .
	poetry run black .

.PHONY: start-localstack
start-localstack: ## start localstack
	docker compose up -d

.PHONY: test
test: start-localstack ## run tests
	docker compose up -d
	poetry run coverage run -m unittest discover -s tests -p "test*.py"
	poetry run coverage html
	poetry run coverage report

.PHONY: test-on-aws
test-on-aws: ## run tests on AWS
	TEST_ON_AWS=1 poetry run python -m unittest discover -s tests

.PHONY: stop-localstack
stop-localstack: ## stop localstack
	docker compose down

.PHONY: run
run: ## run locally
	poetry run ec2-slackbot --config config.yaml

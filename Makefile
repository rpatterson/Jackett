# Development, build, and maintenance tasks:

# Defensive settings for make:
#     https://tech.davis-hansson.com/p/make/
SHELL:=bash
.ONESHELL:
.SHELLFLAGS:=-eu -o pipefail -c
.SILENT:
.DELETE_ON_ERROR:
MAKEFLAGS+=--warn-undefined-variables
MAKEFLAGS+=--no-builtin-rules
PS1?=$$

# Finished with `$(shell)`, echo recipe commands going forward
.SHELLFLAGS+= -x


### Top-level targets:

.PHONY: all
## The default target.
all: build

.PHONY: build
## Set up everything for development from this checkout.
build: ./build/log/tox-py38-update.log \
		./build/log/npm-install.log \
		./.git/hooks/pre-commit

.PHONY: start
## Run the local development end-to-end stack services in the background as daemons.
start: build
	docker compose up --pull="always" -d

.PHONY: run
## Run the local development end-to-end stack services in the foreground for debugging.
run: build
	$(MAKE) start
# Scrollback to the container start on a fresh start:
	docker compose logs -ft --tail="100"

.PHONY: test
## Run all checks and test that can be run locally.
test: lint-yaml validate-definitons

.PHONY: lint-yaml
## Check the YAML style of the indexer definitions.
lint-yaml: ./build/log/tox-py38-update.log
	tox exec -e "$(<:build/log/tox-%-update.log=%)" -- \
	    yamllint -c "./yamllint.yml" "./src/Jackett.Common/Definitions/"

.PHONY: validate-definitons
## Validate the YAML schema of the indexer definitions.
validate-definitons: ./build/log/npm-install.log
	~/.nvm/nvm-exec npm run "validate:definitons"

### Real Targets:

# Manage Python tools:
./.git/hooks/pre-commit: ./build/log/tox-py38-update.log
	tox exec -e "$(<:build/log/tox-%-update.log=%)" -- \
	    pre-commit install --hook-type "pre-commit" --hook-type "commit-msg" \
	    --hook-type "pre-push"
./build/log/tox-py38-update.log: ./requirements.txt ./tox.ini
	$(MAKE) "./build/log/tox-install.log"
	mkdir -pv "$(dir $(@))"
	tox run -e "$(@:build/log/tox-%-update.log=%)" --notest | tee -a "$(@)"
./build/log/tox-install.log:
	$(MAKE) "./build/log/pipx-install.log"
	mkdir -pv "$(dir $(@))"
# https://tox.wiki/en/latest/installation.html#via-pipx
	pipx install "tox" | tee -a "$(@)"
./build/log/pipx-install.log:
	mkdir -pv "$(dir $(@))"
	which -a "pipx" | tee -a "$(@)" || (
	    set +x
	    echo "ERROR:Install pipx: https://pipx.pypa.io/stable/#install-pipx"
	    exit 1
	)

# Manage JavaScript tools:
./build/log/npm-install.log: ./package.json
	$(MAKE) "./build/log/nvm-install.log"
	mkdir -pv "$(dir $(@))"
	~/.nvm/nvm-exec npm install | tee -a "$(@)"
./build/log/nvm-install.log: ./.nvmrc
	$(MAKE) "$(HOME)/.nvm/nvm.sh"
	mkdir -pv "$(dir $(@))"
	set +x
	. "$(HOME)/.nvm/nvm.sh" || true
	nvm install | tee -a "$(@)"
# https://github.com/nvm-sh/nvm#install--update-script
$(HOME)/.nvm/nvm.sh:
	set +x
	wget -qO- "https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh" |
	    bash

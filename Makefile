.PHONY: dev dev-release test test-quick bench clean rust-test check

dev:
	maturin develop

dev-release:
	maturin develop --release

test: dev
	pytest tests/ -v

test-quick:
	pytest tests/ -v --no-header -q

bench: dev-release
	python benchmarks/bench_hello.py

clean:
	cargo clean
	rm -rf dist/ build/ *.egg-info

rust-test:
	cargo test

check:
	cargo clippy -- -D warnings
	ruff check python/ tests/

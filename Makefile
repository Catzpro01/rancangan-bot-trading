PY ?= python3

.PHONY: test test-py test-js workflows validate vectors simulate clean

test: test-py test-js validate contract deploy   ## jalankan seluruh pemeriksaan

test-py:                         ## unit test kernel risiko (Python)
	$(PY) -m pytest -q

test-js:                         ## port JS, adapter MiroFish, kode dalam workflow
	node --test tests/test_parity.js tests/test_mirofish_adapter.js \
	  tests/test_workflow_code_nodes.js tests/test_mirofish_verdict_parity.js

workflows:                       ## regenerasi n8n/workflows/*.json dari generator
	$(PY) tools/make_n8n_workflows.py

validate:                        ## validasi struktur workflow n8n
	$(PY) tools/validate_workflows.py

contract:                        ## validasi kontrak runner<->adapter<->skema<->dokumen
	$(PY) tools/validate_mirofish_contract.py

deploy:                          ## validasi berkas deployment (compose, Dockerfile, .env)
	$(PY) tools/validate_deploy.py

vectors:                         ## regenerasi vektor paritas Python<->JS
	$(PY) tools/make_parity_vectors.py

vectors-check:
	$(PY) tools/make_parity_vectors.py --check

simulate:                        ## Monte Carlo dengan config saat ini
	$(PY) simulator/monte_carlo.py --equity 1000 --leverage 50 \
	  --win-rate 0.50 --stop-pct 0.008 --tp-pct 0.010 --liq-prob 0.30

clean:
	rm -rf .pytest_cache **/__pycache__

"""Evaluation: run the agent on every scenario and grade what it left behind.

python -m evals.run --mode scripted            # golden trajectories, offline (CI)
python -m evals.run --mode replay              # recorded Gemini runs, no key needed
python -m evals.run --mode live --repeats 3    # real Gemini calls
"""

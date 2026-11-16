from database.seed import generate_dataset


def test_real_sql_components_are_measured_separately_from_agent(tmp_path):
    from eval.probes import run_probes
    from eval.eval import evaluate_cases
    root = tmp_path / "data"
    generate_dataset(root, seed=999999, variant="perturbation")
    observations = run_probes(root)
    assert len(observations) == 11
    assert all(o["evidence"]["scope"] == "component" for o in observations.values())
    report = evaluate_cases(root, observations)
    assert report["summary"]["passed"] == 11
    assert report["summary"]["not_run"] == 25
    assert report["safety_gate"] == "NOT_RUN"
    assert report["semantic"]["status"] == "NOT_RUN"

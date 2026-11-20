# Bench datasets (not in git, except the dataset.yaml files)

One folder per dataset, each with a `dataset.yaml` (name, source_url, licence, dataset_class: public, files,
mapping to the standard columns, truth, sample, expected). Download the files by hand into the folder, open
them, list the real columns, and fill in `files` and `mapping` where they say TODO. Truth columns are for
evaluation only: never map them as case_id, l1-l5, actor or text.

    python scripts/bench.py --ai fake                # every dataset that is present
    python scripts/bench.py --datasets icc_like_A --max-rows 20000

Results: `_bench/<date>/bench.md` and `bench.json`. A dataset that is not downloaded is listed, never an error.

# Releasing benchx

Pushing a tag `v<version>` runs `.github/workflows/release.yml`. It builds and tests the
package, waits for approval, publishes to PyPI, and creates the GitHub release. Below,
`upstream` is your remote for `benchx-dev/benchx`, and `0.1.0` stands for the version.

## Checklist

1. **Check CI.** The release workflow runs its own tests, but it does not look at the other
   workflows, so check that the latest commit on `main` is green.
2. **Prepare the release in a pull request, and merge it.**
    - Set `__version__` in `src/benchx/__init__.py`; `pyproject.toml` reads it from there.
    - Update `CHANGELOG.md`, including the list of contributors.
3. **Tag the merged commit and push the tag.** The tag must be `v` plus exactly the version in
   `__version__`, or the workflow stops before uploading anything.

    ```console
    $ git fetch upstream
    $ git tag -a v0.1.0 -m "benchx 0.1.0" upstream/main
    $ git push upstream v0.1.0
    ```

4. **Approve the `publish-pypi` job.** Once the tests pass, the
   [workflow run](https://github.com/benchx-dev/benchx/actions/workflows/release.yml) waits and
   shows a "Review deployments" button. A required reviewer of the `pypi` environment other than
   whoever pushed the tag has to approve it, so a release takes two people.
5. **Check the result.** The run should be green, with the version on
   [PyPI](https://pypi.org/project/benchx/) and in the
   [GitHub releases](https://github.com/benchx-dev/benchx/releases).
6. **Test the install in a new environment.** Activate it, because the tests start benchmarks
   with whichever `python` is on `PATH`.

    ```console
    $ python -m venv venv && source venv/bin/activate
    $ pip install 'benchx[test]==0.1.0'
    $ bx --help
    $ python -m pytest --pyargs benchx
    ```

7. **Merge the conda-forge update.** Conda packages are built from
   [benchx-feedstock](https://github.com/conda-forge/benchx-feedstock), not from this repository.
   After a new version reaches PyPI, conda-forge's bot opens a pull request there, which can take
   several hours. A maintainer of the feedstock then:
    - Compares the recipe's dependencies with `dependencies` in `pyproject.toml`, and updates the
      pull request if they changed. `jsonschema[format-nongpl]` is called
      `jsonschema-with-format-nongpl` on conda-forge.
    - Merges the pull request once its CI passes.
    - Tests the install, once the package is available:

    ```console
    $ conda create -n benchx-test -c conda-forge benchx=0.1.0
    $ conda run -n benchx-test bx --help
    ```

8. **Start the next development version.** In a pull request, set `__version__` to the next
   version with a `.dev0` suffix: after `0.1.0`, that is `0.2.0.dev0`. Results record this version,
   so without this step, results from `main` would claim to come from the release.

## Pre-releases

A version such as `0.1.0rc1`, tagged `v0.1.0rc1`, follows steps 1 to 6. It is marked as a
pre-release on GitHub, and `pip install benchx` skips it unless the version is named or
`--pre` is given.

## If something fails

- **Before the upload:** nothing is published. Delete the tag, fix the problem, and tag again:

    ```console
    $ git push upstream --delete v0.1.0
    $ git tag -d v0.1.0
    ```

- **During the upload to PyPI:** check the [PyPI project](https://pypi.org/project/benchx/)
  first. If the version is not there, re-run the failed job.
- **After the upload to PyPI, in `github-release`:** the package is published, so re-run the
  failed job.
- **A published version is wrong:** PyPI does not allow a version to be uploaded again, even
  after it is deleted, so release a new version. Yanking the bad one on PyPI stops new installs
  from picking it up by default.
- **In conda-forge's pull request:** the release on PyPI is not affected. Push the fix to the
  bot's branch, or open a pull request from a fork of the feedstock.
- **A conda-forge package is wrong, but the version is right:** fix the recipe in a pull request
  on the feedstock and increase its build number by 1. A new version resets it to 0.
- **The bot opens no pull request:** it stops once the feedstock has three open version updates,
  so close or merge those. See conda-forge's
  [guide to updating packages](https://conda-forge.org/docs/maintainer/updating_pkgs/).

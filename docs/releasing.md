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

4. **Approve the `publish-pypi` job** when the
   [workflow run](https://github.com/benchx-dev/benchx/actions/workflows/release.yml) asks. Any
   required reviewer of the `pypi` environment can approve it, including whoever pushed the tag.
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

7. **Merge the conda-forge update.** Conda packages come from the benchx feedstock on
   conda-forge, not from this repository. Once that feedstock exists, conda-forge's bot opens a
   pull request on it after a new version reaches PyPI; review and merge that pull request.

## Pre-releases

A version such as `0.1.0rc1`, tagged `v0.1.0rc1`, follows the same steps. It is marked as a
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

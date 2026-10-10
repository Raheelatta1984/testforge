# TestForge library

This folder is the catalog. The dashboard lists projects, variables, and recordings from here and ignores database rows that are not in this tree. Creating a project, saving a variable, or pressing SAVE on a recording writes the files and pushes them to the current Git branch.

```
library/
  catalog.json
  projects/
    <project-id>/
      project.json
      variables.json
      recordings/
        <recording-id>/
          recording.json
          resources/
            Jenkinsfile
            step-001.jpg
```

`recording.json` holds the steps. `resources/` holds files the recording depends on, including the Jenkins script and any step screenshots. Run videos stay in the artifacts directory; they are execution output, not the library.

`qa-sample-app` is the harness project. `python -m tests.harness` copies this tree and replays `qa-hello-literal` and `qa-hello-variable`. Update those recordings when the sample app or the replay rules change.

Secret variable values are stored in this tree because replay reads them from the repository. Do not put a production password in a public repository.

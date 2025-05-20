#!/bin/bash

# `$1` is the path to the `git-rebase-todo` file provided by Git
TODO_FILE=$1

# Add a 'break' at the top of the file
if [[ -f "$TODO_FILE" ]]; then
    # Prepend 'break' to the file content
    echo -e "break\n$(cat "$TODO_FILE")" > "$TODO_FILE"
    echo "Added break to the rebase-todo file."
else
    echo "Error: rebase-todo file not found" >&2
    exit 1
fi
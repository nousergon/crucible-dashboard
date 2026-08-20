#!/bin/bash
cd ~/Development/alpha-engine-dashboard/.claude/worktrees/fix-blog-build-add-intro-post
git checkout worktree-fix-blog-build-add-intro-post
git cherry-pick 8ebf937
git checkout main
git reset --hard origin/main

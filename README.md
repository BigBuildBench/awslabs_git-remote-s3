# git-remote-s3

This library enables to use Amazon S3 as a git remote and LFS server.

It provides an implementation of a [git remote helper](https://git-scm.com/docs/gitremote-helpers) to use S3 as a serverless Git server.

It also provide an implementation of the [git-lfs custom transfer](https://github.com/git-lfs/git-lfs/blob/main/docs/custom-transfers.md) to enable pushing LFS managed files to the same S3 bucket used as remote.

## Installation

`git-remote-s3` is a Python script and works with any Python version >= 3.9.

Run:

```
pip install git-remote-s3
```

## Prerequisites

Before you can use `git-remote-s3`, you must:

- Complete initial configuration:

  - Creating an AWS account
  - Configuring an IAM user or role

- Create an AWS S3 bucket (or have one already) in your AWS account.
- Attach a minimal policy to that user/role that allows the to the S3 bucket:

  ```json
  {
    "Sid": "S3Access",
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
    "Resource": ["arn:aws:s3:::<BUCKET>", "arn:aws:s3:::*/*"]
  }
  ```

- Optional (but recommended) - use [SSE-KMS Bucket keys to encrypt the content of the bucket](https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucket-key.html), ensure the user/role create previously has the permission to access and use the key.

```json
{
  "Sid": "KMSAccess",
  "Effect": "Allow",
  "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
  "Resource": ["arn:aws:kms:<REGION>:<ACCOUNT>:key/<KEY_ID>"]
}
```

- Install Python and its package manager, pip, if they are not already installed. To download and install the latest version of Python, [visit the Python website](https://www.python.org/).
- Install Git on your Linux, macOS, Windows, or Unix computer.
- Install the latest version of the AWS CLI on your Linux, macOS, Windows, or Unix computer. You can find instructions [here](https://docs.aws.amazon.com/cli/latest/userguide/installing.html).

## Security

### Data encryption

All data is encrypted at rest and in transit by default. To add an additional layer of security you can use customer managed KMS keys to encrypt the data at rest on the S3 bucket. We recommend to use [Bucket keys](https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucket-key.html) to minimize the KMS costs.

### Access control

Access control to the remote is ensured via IAM permissions, and can be controlled at:

- bucket level
- prefix level (you can use prefixes to store multiple repos in the same S3 bucket thus minimizing the setup effort)
- KMS key level

# Use S3 remotes

## Create a new repo

S3 remotes are identified by the prefix `s3://` and at the bare minimum specify the name of the bucket. You can also provide a key prefix as in `s3://my-git-bucket/my-repo` and a profile `s3://my-profile@my-git-bucket/myrepo`.

```bash
mkdir my-repo
cd my-repo
git init
git remote add origin s3://my-git-bucket/my-repo
```

You can then add a file, commit and push the changes to the remote:

```bash
echo "Hello" > hello.txt
git add -A
git commit -a -m "hello"
git push --set-upstream origin main
```

The remote HEAD is set to track the branch that has been pushed first to the remote repo. To change the remote HEAD branch, delete the HEAD object `s3://<bucket>/<prefix>/HEAD` and then run `git-remote-s3 doctor s3://<bucket>/<prefix>`.

## Clone a repo

To clone the repo to another folder just use the normal git syntax using the s3 URI as remote:

```bash
git clone s3://my-git-bucket/my-repo my-repo-clone
```

## Branches, etc.

Creating branches and pushing them works as normal:

```bash
cd my-repo
git checkout -b new_branch
touch new_file.txt
git add -A
git commit -a -m "new file"
git push origin new_branch
```

All git operations that do not rely on communication with the server should work as usual (eg `git merge`)

# LFS

To use LFS you need to first install git-lfs. You can refer to the [official documentation](https://git-lfs.com/) on how to do this on your system.

Next, you need enable the S3 integration by running the following command in the repo folder:

```bash
lfs-s3-py install
```

which is a short cut for:

```bash
git config --add lfs.customtransfer.lfs-s3-py.path lfs-s3-py
git config --add lfs.standalonetransferagent lfs-s3-py
```

## Example use

### Creating the repo and pushing

Let's assume we want to store TIFF file in LFS.

```bash
mkdir lfs-repo
cd lfs-repo
git init
git lfs install
lfs-s3-py install
git lfs track "*.tiff"
git add .gitattributes
<put file.tiff in the repo>
git add file.tiff
git commit -a -m "my first tiff file"
git remote add origin s3://my-git-bucket/lfs-repo
git push --set-upstream origin main
```

## Notes about specific behaviors of Amazon S3 remotes

### Arbitrary Amazon S3 URIs

An Amazon S3 URI for an valid bucket and an arbitrary prefix which does not contain the right structure under it, is considered valid.

`git ls-remote` returns an empty list and `git clone` clones an empty repository for which the S3 URI is set as remote origin.

```
% git clone s3://my-git-bucket/this-is-a-new-repo
Cloning into 'this-is-a-new-repo'...
warning: You appear to have cloned an empty repository.
% cd this-is-a-new-repo
% git remote -v
origin  s3://my-git-bucket/this-is-a-new-repo (fetch)
origin  s3://my-git-bucket/this-is-a-new-repo (push)
```

**Tip**: This behavior can be used to quickly create a new git repo.

## Concurrent writes

Due to the distributed nature of `git`, there might be cases (albeit rare) where 2 or more `git push` are executed at the same time by different user with their own modification of the same branch.

The git command executes the push in 2 steps:

1. first it checks if the remote reference is the correct ancestor for the commit being pushed
2. if that is correct it invokes the `git-remote-s3` command which writes the bundle to the S3 bucket at the `refs/heads/<branch>` path

In case two (or more) `git push` command are executed at the same time from different clients, at step 1 the same valid ref is fetched, hence both clients proceed with step 2, resulting in multiple bundles being stored in S3.

The branch has now multiple head references, and any subsequent `git push` fails with the error:

```
error: dst refspec refs/heads/<branch>> matches more than one
error: failed to push some refs to 's3://<bucket>/<prefix>'
```

To fix this issue, run the `git-remote-s3 doctor <s3-uri>` command. By default it will create a new branch for every bundle that should not be retained. The user can then checkout the branch locally and merge it to the original branch. If you want instead to remove the bundle, specify `--delete-bundle`.

### Clone the repo

When cloning a repo using the S3 remote for LFS, `git-lfs` can't know how to fetch the files since we have yet to add the configuration.

It involves 2 extra steps.

```bash
% git clone s3://my-git-bucket/lfs-repo lfs-repo-clone
Error downloading object: file.tiff (54238cf): Smudge error: Error downloading file.tiff (54238cfaaaa42dda05da0e12bf8ee3156763fa35296085ccdef63b13a87837c5): batch request: ssh: Could not resolve hostname s3: Name or service not known: exit status 255
...
```

To fix:

```bash
cd lfs-repo-clone
lfs-s3-py install
git reset --hard main
```

# Manage the Amazon S3 remote

## Delete branches

To remove remote branches that are not used anymore you can use the `git-s3 delete-branch <s3uri> -b <branch_name>` command. This command deletes the bundle object(s) from Amazon S3 under the branch path.

## Protected branches

To protect/unprotect a branch run `git s3 protect <remote> <branch-name>` respectively `git s3 unprotect <remote> <branch-name>`.

# Under the hood

## How S3 remote work

Bundles are stored in the S3 bucket as `<prefix>/<ref>/<sha>.bundle`.

When listing remote ref (eg explicitly via `git ls-remote`) we list all the keys present under the given <prefix>.

When pushing a new ref (eg a commit), we get the sha of the ref, we bundle the ref via `git bundle create <sha>.bundle <ref>` and store it to S3 according the schema above.

If the push is successful, the code removes the previous bundle associated to the ref.

If two user concurrently push a commit based on the same current branch head to the remote both bundles would be written to the repo and the current bundle removed. No data is lost, but no further push will be possible until all bundles but one are removed.
For this you can use the `git s3 doctor <remote>` command.

## How LFS work

The LFS integration stores the file in the bucket defined by the remote URI, under a key `<prefix>/lfs/<oid>`, where oid is the unique identifier assigned by git-lfs to the file.

If an object with the same key already exists, git-lfs-s3 does not upload it again.

## Debugging

Use `--verbose` flag to print some debug information when performing git operations. Logs will be put to stderr.

For LFS operations you can enable and disable debug logging via `git-lfs-s3 enable-debug` and `git-lfs-s3 disable-debug` respectively. Logs are put in `.git/lfs/tmp/git-lfs-s3.log` in the repo.

# Credits

The git S3 integration was inspired by the work of Bryan Gahagan on [git-remote-s3](https://github.com/bgahagan/git-remote-s3).

The LFS implementation benefitted from [lfs-s3](https://github.com/nicolas-graves/lfs-s3) by [@nicolas-graves](https://github.com/nicolas-graves). If you do not need to use the git-remote-s3 transport you are should use that project.

# SPDX-FileCopyrightText: 2023-present Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0

import sys
import logging
import boto3
import boto3.exceptions
from botocore.exceptions import (
    ClientError,
    ProfileNotFound,
    CredentialRetrievalError,
    NoCredentialsError,
    UnknownCredentialError,
)
import re
import tempfile
import os
from git_remote_s3 import git
from .common import parse_git_url
import botocore

logger = logging.getLogger(__name__)
if "remote" in __name__:
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)


class Mode:
    FETCH = "fetch"
    PUSH = "push"


class S3Remote:
    def __init__(self, profile, bucket, prefix):
        self.profile = profile
        self.bucket = bucket
        self.prefix = prefix
        if profile:
            self.session = boto3.Session(profile_name=profile)
        else:
            self.session = boto3.Session()
        self.s3 = self.session.client("s3")
        self.bucket = bucket
        self.mode = None
        self.fetched_refs = []
        self.push_cmds = []

    def list_refs(self, *, bucket: str, prefix: str) -> list:
        res = self.s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        contents = res.get("Contents", [])
        next_token = res.get("NextContinuationToken", None)

        while next_token:
            res = self.s3.list_objects_v2(
                Bucket=bucket, Prefix=prefix, ContinuationToken=next_token
            )
            contents.extend(res.get("Contents", []))
            next_token = res.get("NextContinuationToken", None)

        contents = self.s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get(
            "Contents", []
        )
        contents.sort(key=lambda x: x["LastModified"])
        contents.reverse()

        objs = [
            o["Key"][o["Key"].index("/") + 1 :]
            for o in contents
            if o["Key"].startswith(prefix + "/refs") and o["Key"].endswith(".bundle")
        ]
        return objs

    def cmd_fetch(self, args: str):
        sha, ref = args.split(" ")[1:]
        if sha in self.fetched_refs:
            return
        logger.info(f"fetch {sha} {ref}")
        try:
            obj = self.s3.get_object(
                Bucket=self.bucket, Key=f"{self.prefix}/{ref}/{sha}.bundle"
            )
            data = obj["Body"].read()

            temp_dir = tempfile.mkdtemp(prefix="git_remote_s3_fetch_")
            with open(f"{temp_dir}/{sha}.bundle", "wb") as f:
                f.write(data)
            logger.info(f"fetched {temp_dir}/{sha}.bundle {ref}")

            git.unbundle(folder=temp_dir, sha=sha, ref=ref)
            self.fetched_refs.append(sha)
        finally:
            if os.path.exists(f"{temp_dir}/{sha}.bundle"):
                os.remove(f"{temp_dir}/{sha}.bundle")

    def cmd_push(self, args: str):
        force_push = False
        local_ref, remote_ref = args.split(" ")[1].split(":")
        if local_ref.startswith("+"):
            force_push = not self.is_protected(remote_ref)
            logger.info(f"Force push {force_push}")
            local_ref = local_ref[1:]

        logger.info(f"push {local_ref} {remote_ref}")
        temp_dir = tempfile.mkdtemp(prefix="git_remote_s3_push_")

        contents = self.get_bundles_for_ref(remote_ref)
        if len(contents) > 1:
            return f'error {remote_ref} "multiple bundles exists on server. Run git-s3 doctor to fix."?\n'  # noqa: B950

        remote_to_remove = contents[0]["Key"] if len(contents) == 1 else None

        try:
            sha = git.rev_parse(local_ref)
            if remote_to_remove:
                remote_sha = remote_to_remove.split("/")[-1].split(".")[0]
                if not force_push and not git.is_ancestor(remote_sha, sha):
                    return f'error {remote_ref} "remote ref is not ancestor of {local_ref}."?\n'

            temp_file = git.bundle(folder=temp_dir, sha=sha, ref=local_ref)

            with open(temp_file, "rb") as f:
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=f"{self.prefix}/{remote_ref}/{sha}.bundle",
                    Body=f,
                )
            self.init_remote_head(remote_ref)
            logger.info(f"pushed {temp_file} to {remote_ref}")
            if remote_to_remove:
                self.s3.delete_object(Bucket=self.bucket, Key=remote_to_remove)

            return f"ok {remote_ref}\n"
        except git.GitError:
            logger.info(f"fatal: {local_ref} not found\n")
            return f'error {remote_ref} "{local_ref} not found"?\n'
        except boto3.exceptions.S3UploadFailedError as e:
            logger.info(f"fatal: {e}\n")
            return f'error {remote_ref} "{e}"?\n'
        except botocore.exceptions.ClientError as e:
            logger.info(f"fatal: {e}\n")
            return f'error {remote_ref} "{e}"?\n'
        finally:
            if os.path.exists(f"{temp_dir}/{sha}.bundle"):
                os.remove(f"{temp_dir}/{sha}.bundle")

    def init_remote_head(self, ref: str) -> None:
        """Initialise the remote HEAD reference if it does not exist

        Args:
            ref (str): The ref to which the remote HEAD should point to
        """
        if not self.s3.list_objects_v2(
            Bucket=self.bucket, Prefix=f"{self.prefix}/HEAD"
        ).get("Contents", []):
            self.s3.put_object(
                Bucket=self.bucket,
                Key=f"{self.prefix}/HEAD",
                Body=ref,
            )

    def get_bundles_for_ref(self, remote_ref: str) -> list[str]:
        """Lists all the bundles for a given ref on the remote

        Args:
            remote_ref (str): the remote ref

        Returns:
            list[str]: the list of bundle keys
        """
        return [
            c
            for c in self.s3.list_objects_v2(
                Bucket=self.bucket, Prefix=f"{self.prefix}/{remote_ref}/"
            ).get("Contents", [])
            if "PROTECTED#" not in c["Key"]
        ]

    def is_protected(self, remote_ref):
        protected = self.s3.list_objects_v2(
            Bucket=self.bucket, Prefix=f"{self.prefix}/{remote_ref}/PROTECTED#"
        ).get("Contents", [])
        return protected

    def cmd_option(self, arg: str):
        option, value = arg.split(" ")[1:]
        if option == "verbosity" and int(value) >= 2:
            logger.setLevel(logging.INFO)
            sys.stdout.write("ok\n")
        else:
            sys.stdout.write("unsupported\n")
        sys.stdout.flush()

    def cmd_list(self, *, for_push: bool = False):
        objs = self.list_refs(bucket=self.bucket, prefix=self.prefix)
        logger.info(objs)

        if not for_push:
            try:
                head = self.get_remote_head()
                logger.info(f"HEAD=[{head}]")
                for o in objs:
                    ref = "/".join(o.split("/")[:-1])
                    if ref == head:
                        logger.info(f"@{ref} HEAD\n")
                        sys.stdout.write(f"@{ref} HEAD\n")
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchKey":
                    pass  # ignoring missing HEAD on remote

        for o in [x for x in objs if re.match(".+/.+/.+/[a-f0-9]{40}.bundle", x)]:
            elements = o.split("/")
            sha = elements[-1].split(".")[0]
            sys.stdout.write(f"{sha} {'/'.join(elements[:-1])}\n")

        sys.stdout.write("\n")
        sys.stdout.flush()

    def get_remote_head(self) -> str:
        """Gets the remote head ref

        Returns:
            str: the remote head ref
        """
        head = (
            self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}/HEAD")
            .get("Body")
            .read()
            .decode("utf-8")
            .strip()
        )

        return head

    def cmd_capabilities(self):
        sys.stdout.write("*push\n")
        sys.stdout.write("*fetch\n")
        sys.stdout.write("option\n")
        sys.stdout.write("\n")
        sys.stdout.flush()

    def process_cmd(self, cmd: str):  # noqa: C901
        if cmd.startswith("fetch"):
            self.cmd_fetch(cmd.strip())
        elif cmd.startswith("push"):
            if self.mode != Mode.PUSH:
                self.mode = Mode.PUSH
                self.push_cmds = []
            self.push_cmds.append(cmd.strip())
            # self.cmd_push(cmd.strip())
        elif cmd.startswith("option"):
            self.cmd_option(cmd.strip())
        elif cmd.startswith("list for-push"):
            self.cmd_list(for_push=True)
        elif cmd.startswith("list"):
            self.cmd_list()
        elif cmd.startswith("capabilities"):
            self.cmd_capabilities()
        elif cmd == "\n":
            logger.info("empty line")
            if self.mode == Mode.PUSH and self.push_cmds:
                logger.info(f"pushing {self.push_cmds}")
                push_res = [self.cmd_push(c) for c in self.push_cmds]
                for res in push_res:
                    sys.stdout.write(res)
                self.push_cmds = []
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            sys.stderr.write(f"fatal: invalid command '{cmd}'\n")
            sys.stderr.flush()
            sys.exit(1)


def main():
    logger.info(sys.argv)
    remote = sys.argv[2]
    profile, bucket, prefix = parse_git_url(remote)
    if bucket is None or prefix is None:
        sys.stderr.write(
            f"fatal: invalid remote '{remote}'. You need to have a bucket and a prefix.\n"
        )
        sys.exit(1)
    try:
        s3remote = S3Remote(profile=profile, bucket=bucket, prefix=prefix)
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            logger.info(f"cmd: {line}")
            s3remote.process_cmd(line)

    except BrokenPipeError:
        logger.info("BrokenPipeError")
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)
    except OSError as err:
        # Broken pipe error on Windows
        # see https://stackoverflow.com/questions/23688492/oserror-errno-22-invalid-argument-in-subprocess # noqa: B950
        if err.errno == 22:
            logger.info("BrokenPipeError")
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            sys.exit(0)
        else:
            raise err
    except (
        ClientError,
        ProfileNotFound,
        CredentialRetrievalError,
        NoCredentialsError,
        UnknownCredentialError,
    ) as e:
        sys.stderr.write(f"fatal: invalid credentials {e}\n")
        sys.stderr.flush()
        sys.exit(1)

#!/usr/bin/env python3

import psycopg2
import psycopg2.extras
import subprocess
import argparse
import os
import docker
import json
import asyncio
from postgrade import post_grade
from itertools import islice

argparser = argparse.ArgumentParser()
argparser.add_argument(
    '--image',
    default='yuvipanda/materials-x18',
    help='Image to use for grading'
)
argparser.add_argument(
    'lab',
    help='Lab to grade'
)
argparser.add_argument(
    'resource_link_id',
    help='Resource Link ID for this lab'
)
argparser.add_argument(
    '--postgres-host',
    default='/run/cloudsql/data8x-scratch:us-central1:prod-hubshard-db-instance',
    help='Hostname to use to connect to postgresql db'
)
argparser.add_argument(
    '--postgres-username',
    default='prod-db-proxyuser',
    help='Username for connecting to postgresql db'
)
argparser.add_argument(
    '--postgres-dbname',
    default='prod-hubshard-sharder-db',
    help='Database to connect to on postgres host'
)

args = argparser.parse_args()

LTI_CONSUMER_KEY = os.environ['LTI_CONSUMER_KEY']
LTI_CONSUMER_SECRET = os.environ['LTI_CONSUMER_SECRET']

docker_client = docker.from_env()

async def main():
    conn = psycopg2.connect(
        host=os.path.abspath(args.postgres_host),
        user=args.postgres_username,
        dbname=args.postgres_dbname,
        password=os.environ['POSTGRES_PASSWORD'],
        cursor_factory=psycopg2.extras.DictCursor
    )

    with conn.cursor() as cur:
        cur.execute(
            "select * from lti_launch_info_v1 where resource_link_id=%s",
            (args.resource_link_id, )
        )
        grade_coros = (
            grade_lab(row['user_id'], row['launch_info'], args.lab, args.image)
            for row in cur
        )

        for res in limited_as_completed(grade_coros, 16):
            await res

def limited_as_completed(coros, limit):
    futures = [
        asyncio.ensure_future(c)
        for c in islice(coros, 0, limit)
    ]
    async def first_to_finish():
        while True:
            await asyncio.sleep(0)
            for f in futures:
                if f.done():
                    futures.remove(f)
                    try:
                        newf = next(coros)
                        futures.append(
                            asyncio.ensure_future(newf))
                    except StopIteration as e:
                        pass
                    return f.result()
    while len(futures) > 0:
        yield first_to_finish()

async def grade_lab(user_id, launch_info, lab, grader_image):
    src_path = f"{user_id}/materials-x18/materials/x18/lab/1/{lab}/{lab}.ipynb"
    if not os.path.exists(src_path):
        # The princess is in another file server, mario
        print(f"skipping {user_id}")
        return

    command = [
        'docker', 'run',
        '--rm',
        '-m', '1G',
        '-i',
        '--net=none',
        grader_image,
        "/srv/repo/grading/containergrade.bash",
        f"/srv/repo/materials/x18/lab/1/{lab}/{lab}.ipynb)"
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    with open(src_path) as f:
        content = f.read().encode('utf-8')
        stdout, stderr = await process.communicate(content)
        for line in stderr.decode('utf-8').split('\n'):
            if not line.startswith('WARNING:'):
                print(line)
    grade = float(stdout)
    if grade != 0.0:
        await post_grade(
            launch_info['lis_result_sourcedid'],
            launch_info['lis_outcome_service_url'],
            LTI_CONSUMER_KEY,
            LTI_CONSUMER_SECRET,
            grade
        )
        print(f"posted {user_id} with grade {grade}")

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    # Run the commands
    loop.run_until_complete(main())
    loop.close()
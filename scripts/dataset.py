from api import CodeforcesAPI, LeetCodeAPI
import mysql.connector
from mysql.connector.errors import IntegrityError

import logging
from typing import Container, Iterable, cast


# TODO utilize logging framework instead of `print()`


class CodeforcesDatasetBuilder:
    AUTHOR_BLOCK_SIZE = 512
    SUBMISSION_BLOCK_SIZE = 2048

    def __init__(self) -> None:
        self.cnx = mysql.connector.connect(user="root", password="root")
        self.cursor = self.cnx.cursor()  # Used for processing queries
        self.cursor.execute("USE horizon_initiative;")
        self.api = CodeforcesAPI()  # Connect to the codeforces API

    def load_metadata(self, contests: list[int]) -> None:
        for contest_id in contests:
            contest = self._fetch_contest_info(contest_id)
            # contest = (id, name, start_time, duration)
            end_time = contest[2] + contest[3]

            print("Fetching participant information")
            participants = self._fetch_contest_standings(contest_id)
            print("Fetching submission information")
            self._fetch_contest_submissions(contest_id, end_time, participants)

    def _fetch_contest_info(
        self, contest_id: int, force=False
    ) -> tuple[int, str, int, int]:
        if not force and self._is_known_contest(contest_id):
            return  # Don't update the entry if it already exists

        retval = self.api.get_contest_standings(contest_id, count=1)
        assert retval["status"] == "OK", f"Invalid API response: {retval['status']}"
        contest = retval["result"]["contest"]

        contest_name = contest["name"]
        contest_start_time = contest["startTimeSeconds"]
        contest_duration = contest["durationSeconds"]
        query = "INSERT INTO codeforces_contest (id, name, start_time, duration) VALUES (%s, %s, %s, %s)"
        values = (contest_id, contest_name, contest_start_time, contest_duration)

        self.cursor.execute(query, values)  # INSERT INTO [...] VALUES [...]
        return values  # Return the contest information, to prevent query

    def _fetch_contest_standings(self, contest_id: int) -> set[str]:
        participants = set()

        offset = 1  # Used to store the last row fetched
        while True:
            # For each block of requested rankings, we will obtain relevent
            # author metadata. Afterwords, we will obtain all associated
            # submission metadata with one request. As a result, this is able
            # to reduce the number of API calls by a considerable amount
            handles = []

            retval = self.api.get_contest_standings(
                contest_id, offset, self.AUTHOR_BLOCK_SIZE
            )
            assert retval["status"] == "OK", f"Invalid API response: {retval['status']}"
            contest_standings = retval["result"]

            for row in contest_standings["rows"]:
                party_memebers = row["party"]["members"]
                assert len(party_memebers) == 1, "Submission MUST contain one author"
                handles.append(party_memebers[0]["handle"])
            self._fetch_user_info(handles)
            self.cnx.commit()  # Commit all data to the database

            if len(contest_standings["rows"]) < self.AUTHOR_BLOCK_SIZE:
                break  # All participant standings have been recorded

            participants.update(handles)
            offset += self.AUTHOR_BLOCK_SIZE
        return participants

    def _fetch_contest_submissions(
        self,
        contest_id: int,
        end_time: int = None,
        participants: Container[str] = None,
    ) -> None:
        if end_time is None:
            self.cursor.execute(
                f"SELECT * FROM codeforces_contest WHERE id={contest_id}"
            )
            contest = self.cursor.fetchall()[0]  # Calculate the contest end time
            end_time = contest["start_time"] + contest["duration"]

        offset = 1  # Used to store the last row fetched
        while True:
            retval = self.api.get_contest_status(
                contest_id, offset=offset, count=self.SUBMISSION_BLOCK_SIZE
            )
            assert retval["status"] == "OK", "Invalid API response"
            for subm in retval["result"]:
                if len(author := subm["author"]["members"]) > 1:
                    continue

                handle = author[0]["handle"]
                if participants is None and self._is_known_user(handle):
                    continue  # Handle must be in the database (foreign key)
                if participants is not None and handle not in participants:
                    continue  # Ensure the author participated in the contest
                if subm["creationTimeSeconds"] > end_time:
                    continue  # Don't accept submissions after the contest has ended

                verdict = (
                    subm["verdict"] if "verdict" in cast(dict, subm).keys() else ""
                )
                problem = subm["problem"]["index"]  # Increases readability
                try:
                    self.cursor.execute(
                        "INSERT INTO codeforces_submission (id, contest_id, creation_time,"
                        "problem, author_handle, programming_language, verdict) VALUES"
                        f'({subm["id"]}, {contest_id}, {subm["creationTimeSeconds"]}, "{problem}",'
                        f'"{handle}", "{subm["programmingLanguage"]}", "{verdict}")'
                    )
                except IntegrityError:
                    print(f"Duplicate submission detected ({subm['id']})...")
                    print(subm) # Dump out relevent submission information
            self.cnx.commit()  # Commit all data to the database

            if len(retval["result"]) < self.SUBMISSION_BLOCK_SIZE:
                break  # All participant submissions have been recorded
            offset += self.SUBMISSION_BLOCK_SIZE

    def _fetch_user_submissions(self, contest: int, handle: str) -> None:
        retval = self.api.get_contest_status(contest, handle, count=128)
        assert retval["status"] == "OK", "Invalid API response"
        for subm in retval["result"]:
            verdict = subm["verdict"] if "verdict" in cast(dict, subm).keys() else ""
            problem = subm["problem"]["index"]  # Increases readability
            self.cursor.execute(
                "INSERT INTO codeforces_submission (id, contest_id, creation_time,"
                "problem, author_handle, programming_language, verdict) VALUES"
                f'({subm["id"]}, {contest}, {subm["creationTimeSeconds"]}, "{problem}",'
                f'"{handle}", "{subm["programmingLanguage"]}", "{verdict}")'
            )

    def _fetch_user_info(self, handles: str | Iterable[str], force=False) -> None:
        retval = self.api.get_user_info(handles)
        assert retval["status"] == "OK", f"Invalid API response: {retval['status']}"

        for user in retval["result"]:
            user = cast(dict, user)  # Cast for type checking
            if not force and self._is_known_user(user["handle"]):
                continue  # Don't update the entry if it already exists

            country = user["country"] if "country" in user.keys() else ""
            city = user["city"] if "city" in user.keys() else ""
            rating = user["maxRating"] if "maxRating" in user.keys() else 0
            registered = user["registrationTimeSeconds"]
            query = "INSERT INTO codeforces_user (handle, country, city, max_rating, registered) VALUES (%s, %s, %s, %s, %s)"

            values = (user["handle"], country, city, rating, registered)
            self.cursor.execute(query, values)  # INSERT INTO [...] VALUES [...]

    def _is_known_user(self, handle: str) -> bool:
        """Used to determine if a user's metadata exists within the database"""
        self.cursor.execute("SELECT * FROM codeforces_user WHERE handle=%s", (handle,))
        return len(self.cursor.fetchall()) > 0

    def _is_known_contest(self, contest_id: int) -> bool:
        self.cursor.execute(f"SELECT * FROM codeforces_contest WHERE id={contest_id}")
        return len(self.cursor.fetchall()) > 0


class LeetCodeDatasetBuilder:
    def __init__(self) -> None:
        pass

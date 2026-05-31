class CrowdEngine:
    def count_people(self, results):
        count = 0
        confirmed_ids = []

        for box in results[0].boxes:
            if int(box.cls) == 0:
                count += 1
                if box.id is not None:
                    confirmed_ids.append(int(box.id))

        return count, confirmed_ids
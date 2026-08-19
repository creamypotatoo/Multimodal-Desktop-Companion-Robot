from intelligence.memory import remember_encounter, get_person_info

remember_encounter("sera")
remember_encounter("sera")
remember_encounter("mom")

print(get_person_info("sera"))
print(get_person_info("mom"))
print(get_person_info("stranger"))  # should print None
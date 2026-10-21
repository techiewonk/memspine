# Initial predicate vocabulary — accepted design, extraction gated

| Predicate | Subject types | Value kinds | Semantics | Entity-object candidates |
|---|---|---|---|---|
| resides_in | person, agent, group | entity | state | place |
| works_in | person, agent, group | entity | state | place |
| home_country | person, agent, group | entity | relation | place |
| born_in | person, agent, group | entity | relation | place |
| grew_up_in | person, agent, group | entity | relation | place |
| move_origin | person, agent, group | entity | event | place |
| move_destination | person, agent, group | entity | event | place |
| visited | person, agent, group | entity | event | place |
| temporarily_present_in | person, agent, group | entity | event | place |
| parent_of | person | entity | relation | person |
| sibling_of | person | entity | relation | person |
| spouse_of | person | entity | relation | person |
| friend_of | person, agent, group | entity | relation | person |
| member_of | person, agent, group | entity | relation | organisation, group |
| employed_by | person | entity | relation | organisation |
| owns | person, agent, group, organisation | entity, text | state | object, work, project, place |
| adopted | person, agent, group, organisation | entity, text | event | person, object |
| made | person, agent, group, organisation | entity, text | event | object, work, project |
| gave | person, agent, group, organisation | entity, text | event | object, work, project, place |
| received | person, agent, group, organisation | entity, text | event | object, work, project, place |
| commissioned | person, agent, group, organisation | entity, text | event | object, work, project |
| read | person, agent, group, organisation | entity, text | event | work |
| listened_to | person, agent, group, organisation | entity, text | event | work |
| watched | person, agent, group, organisation | entity, text | event | work |
| played | person, agent, group, organisation | entity, text | event | work, object |
| participated_in | person, agent, group, organisation | entity, text | event | work, project, group, organisation |
| attended | person, agent, group, organisation | entity, text | event | work, organisation |
| offered | person, agent, group, organisation | entity, text | event | work, project, object |
| likes | person, agent, group | entity, text | state | object, work, project, person, agent, group |
| dislikes | person, agent, group | entity, text | state | object, work, project, person, agent, group |
| prefers | person, agent, group | entity, text | state | object, work, project, person, agent, group |
| interested_in | person, agent, group | entity, text | state | object, work, project, person, agent, group |
| intends | person, agent, group | entity, text | relation | object, work, project, person, agent, group |
| suggested_to | person, agent, group | entity, text | relation | object, work, project, person, agent, group |
| started_on | person, agent, group, organisation, project, work | temporal | event |  |
| opened_on | person, agent, group, organisation, project, work | temporal | event |  |
| completed_on | person, agent, group, organisation, project, work | temporal | event |  |
| occurred_on | person, agent, group, organisation, project, work | temporal | event |  |
| has_quantity | person, agent, group, organisation, object, project | number, text | attribute |  |
| has_measurement | person, agent, group, organisation, object, project | number, text | attribute |  |
| explicit_frequency | person, agent, group, organisation, object, project | number, text | attribute |  |
| explicit_reason | person, agent, group, organisation, project | text | relation |  |
| has_attribute | person, agent, group, organisation, object, work, project, place | text, number, boolean, entity | attribute | object, work, project, place, species |

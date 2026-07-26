# Maximum stable semantic projection

## Objective

Annotate the richest result that is simultaneously stable, source-grounded, and unambiguous across all supplied captures. The golden is not a transcript and not an attempt to preserve every token.

## Primary entities

- Choose the command's repeated business entity: interfaces, routes, peers, inventory components, rules, processes, and so on.
- Preserve every real primary entity and its source order in each capture. A missed last row, filtered disabled row, or de-duplicated repeated row is an error.
- Do not turn headings, separators, continuation labels, legends, prompts, command echoes, warnings, or pagination controls into entities.
- Nest a secondary collection only when it is clearly owned by a primary entity and is consistently represented.

Ask the human before editing if both a flat and nested representation, or two different primary entities, are equally defensible from the raw text. This is the one ambiguity the Skill must not resolve on its own.

## Stable fields

For each kind of primary entity, start with fine-grained business fields explicitly present in the source. Keep a field only if:

1. its meaning is the same in every supplied format variant;
2. it is present and non-empty for every entity of that kind across every input;
3. its boundary can be identified without borrowing neighboring columns or prose; and
4. retaining it does not require inference, normalization, enrichment, or a lookup.

Exclude an entire optional field when any same-kind entity lacks it, even if most rows contain it. Prefer separate atomic fields over a concatenated row, but do not split a value when the source does not expose a stable boundary.

Never include empty strings, whitespace-only values, `null`, sentinel placeholders, empty containers, row numbers, synthetic IDs, or computed values. Do not normalize case, spacing inside meaningful values, units, addresses, abbreviations, or punctuation unless the source itself supplies the normalized token.

## Types

Default to `string`. Use `integer`, `number`, or `boolean` only when the source semantics and representation make the conversion lossless and uniform across all inputs. Identifiers, interface indexes, VLANs, AS numbers, addresses, versions, values with units, leading-zero values, and mixed numeric/symbolic columns normally remain strings.

Object key order is not scored. Array order, scalar type, missing fields, empty strings, and `null` are all distinct and strictly scored.

## Independent annotation discipline

The raw capture is the only answer source. File names and command descriptions may establish context, but not values. Do not compare against a generated result while annotating. Do not change a golden merely to make a live evaluation pass. A correction requires evidence in the raw capture and should be explainable without reference to Agent behavior.

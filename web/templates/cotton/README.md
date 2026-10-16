# Cotton Component Library

Reusable UI components for WCComps Portal templates using Django Cotton.

## Components

### Layout & Structure

#### `<c-module>`
Content container (card-like) with optional toolbar.
```django
<c-module>
    <c-slot name="toolbar">
        <c-link href="/back">Back</c-link>
    </c-slot>
    Main content here
</c-module>
```

#### `<c-page_header>`
Page title with optional subtitle.
```django
<c-page_header title="Ticket List" subtitle="Team Blue" />
```

#### `<c-fieldset>`
Grouped form fields with heading.
```django
<c-fieldset heading="Ticket Information">
    <c-form_field label="Title" required="true">
        <input type="text" name="title" />
    </c-form_field>
</c-fieldset>
```

### Forms

#### `<c-form_field>`
Form row with label, input, and optional help text/error.
```django
<c-form_field label="Email" required="true" help_text="Enter your work email">
    <input type="email" name="email" />
</c-form_field>
```

#### `<c-filter_toolbar>`
Search and filter form with horizontal layout.
```django
<c-filter_toolbar form_id="ticket-filters">
    <c-filter_field label="Status" id="id_status">
        <select name="status">...</select>
    </c-filter_field>
</c-filter_toolbar>
```

#### `<c-filter_field>`
Individual filter input within toolbar.
```django
<c-filter_field label="Team" id="id_team">
    <input type="number" name="team" />
</c-filter_field>
```

### Interactive Elements

#### `<c-button>`
Styled action buttons.
```django
<c-button variant="primary" type="submit">Save</c-button>
<c-button variant="danger">Delete</c-button>
<c-button variant="cancel">Cancel</c-button>
```

Props:
- `variant`: default|primary|danger|cancel
- `type`: button|submit
- `class`, `id`, `name`, `value`

#### `<c-toggle_button>`
A button that stays pressed while its choice is picked (`pressed="true"` for the initial state); the page's
script calls `setTogglePressed(button, pressed)` from `static/js/utils.js`, which sets its look and `aria-pressed`.
```django
<c-toggle_button data-box="web01">web01</c-toggle_button>
```

#### `<c-link>`
Styled links.
```django
<c-link href="/back">Back</c-link>
<c-link href="/add" variant="add">Add New</c-link>
```

Props: `href`, `variant` (default|primary|add|change|danger|cancel)

### Display

#### `<c-badge>`
Status indicators with color coding.
```django
<c-badge variant="info">Open</c-badge>
<c-badge variant="warning">Claimed</c-badge>
<c-badge variant="success">Resolved</c-badge>
<c-badge variant="neutral">Cancelled</c-badge>
<c-badge variant="{{ ticket.status|badge_variant:'ticket' }}">{{ ticket.get_status_display }}</c-badge>
```

#### `<c-alert>`
Info, warning, error, success messages.
```django
<c-alert variant="error">Something went wrong</c-alert>
<c-alert variant="warning">Points will be deducted</c-alert>
<c-alert variant="success">Ticket created</c-alert>
```

#### `<c-score_value>`
Auto-colored score display with optional signed format.
```django
<c-score_value value='150' />           <!-- Green for positive -->
<c-score_value value='-25' />           <!-- Red for negative -->
<c-score_value value='0' />             <!-- Gray for zero -->
<c-score_value value='50' format='signed' />  <!-- Shows "+50" in green -->
<c-score_value :value="score" />        <!-- From template variable -->
```

Props:
- `value` (required): Numeric value to display
- `format` (optional): "default" (no + prefix) or "signed" (adds + to positive)

Color mapping:
- Positive values: #28a745 (green)
- Negative values: #dc3545 (red)
- Zero: #6c757d (gray)

#### `<c-empty_state>`
Empty state display for when no data is available.
```django
<c-empty_state title="No incidents found" />
<c-empty_state icon="📋" title="No results" />
<c-empty_state
    icon="🔍"
    title="No search results"
    description="Try adjusting your filters.">
    <a href="/reset/" class="button">Clear Filters</a>
</c-empty_state>
```

Props:
- `icon` (optional): Emoji or icon character displayed above title
- `title` (required): Main heading text
- `description` (optional): Secondary explanatory text in muted color
- Slot: Optional action button or link

Use cases: Empty lists, no search results, filtered views with zero matches.

### Tables

#### `<c-table>`
Data table with header and body slots.
```django
<c-table id="results" aria_label="Tickets list" fixed_layout="true">
    <c-slot name="headers">
        <tr>
            <c-table_header sort_field="team" current_sort="-created_at">Team</c-table_header>
        </tr>
    </c-slot>
    <tr><td>Data row</td></tr>
</c-table>
```

Props: `id`, `class`, `aria_label`, `role` (e.g. `grid` for a table whose cells are `gridcell`s), `fixed_layout`,
`widths` (column widths, space separated: `widths="40px 20% 30%"`; template tags may pick them).
Slot: `headers`.

#### `<c-table_header>`
Sortable table column header.
```django
<c-table_header
    sort_field="team__team_number"
    current_sort="{{ sort_by }}"
    query_params="&status={{ status_filter|urlencode }}">
    Team Number
</c-table_header>
```

#### `<c-pagination>`
Page navigation for paginated results.
```django
<c-pagination :page_obj="page_obj" query_params="&status={{ status_filter|urlencode }}&team={{ team_filter|urlencode }}" />
```

`query_params` is the list's filters, written `&name=value` with each value `|urlencode`d, so a value holding `&`
stays one parameter.

## Benefits

1. **Consistency**: UI elements look and behave the same across pages
2. **Maintainability**: Update styling in one place, affects all uses
3. **Reduced Code**: Less repetition in templates (~40% reduction)
4. **Documentation**: Self-documenting with clear prop names

## Attributes and Escaping

A component writes the attributes it doesn't declare in `<c-vars>` with `{{ attrs }}`. django-cotton doesn't escape
them; `core/cotton.py` does, for every component, at startup:
- a value from the page's context (`:title="search"`, `title=search`, `:attrs="dict"`) is escaped;
- a value written `title="{{ search }}"` was escaped by the template engine and isn't escaped again;
- a literal (`title="Tom &amp; Jerry"`, `@click="go(&quot;a&quot;)"`) is the template's own HTML and keeps its entities.

`core/tests/test_cotton_escaping.py` renders a component with values built to break out of their attribute and
fails if one comes out raw; run it after any django-cotton upgrade, since `core/cotton.py` replaces two of its
internals. An Alpine binding on a component is written `::name` (cotton passes it through as `:name`); a `:name`
the page's context doesn't have, on a component that doesn't declare it, fails to render rather than vanishing.
Escaping makes a value safe as attribute text only: request text never goes into `href`, `on*` or Alpine
attributes, where text is a URL or code.

One page's markup that htmx also refreshes (a list's table) is a template beside the page that the page
`{% include %}`s and the view renders on its own, not a component.

## Styling Vocabulary

A page styles itself only with classes: the ones these components render (with their app.css modifiers, such as
`alert--error`), the utilities between `BEGIN UTILITIES` and `END UTILITIES` in `static/css/app.css`, and Django
admin's classes. A class the page's own script adds, or a selector that only mentions a component, is the page's
own. No `style` attributes or `<style>` blocks: a fixed look is a utility, a choice is a component option (such as
`<c-table widths="...">`). Markup a component renders goes through the component, a `variant` is one the component
knows, and a stored status reaches a badge through `|badge_variant:"kind"` (`core/status_variants.py`).
`core/tests/test_template_vocabulary.py` enforces all of this, and fails on an app.css rule no page uses; each
check's exceptions list says why no component or utility fits.

## Migration Strategy

Templates can be gradually refactored to use components:
1. Replace repetitive button HTML with `<c-button>`
2. Replace status spans with `<c-badge>`
3. Replace form rows with `<c-form_field>`
4. Replace tables with `<c-table>` and `<c-table_header>`

The components use Django admin's CSS and app.css.

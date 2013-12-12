import csv
from datetime import timedelta, date, datetime
import itertools
import os
import tempfile
import zipfile

from ask.models import Question
from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext, Context
from django.template.loader import get_template
from django.utils.encoding import smart_unicode
from reversion import revision
from signalbox.decorators import group_required
from signalbox.forms import SelectExportDataForm, DateShiftForm
from signalbox.models import Answer, Membership, Observation, Reply
from signalbox.utilities.djangobits import conditional_decorator
from signalbox.utilities.djangobits import supergetattr
from contracts import contract

EXPORT_DATEFORMAT = "%Y/%m/%d %H:%M:%S"


# The internal accessors are listed first in each tuple, and the name we want
# to export listed second.
FIELD_MAP = [
    ('question.q_type', 'qtype'),
    ('get_value_for_export', 'answer'),
    ('question.variable_name', 'variable_name'),
    ('reply.entry_method', 'entry_method'),
    ('reply.observation.n_in_sequence', 'observation_index'),
    ('reply.observation.due', 'due'),
    ('reply.observation.id', 'observation'),
    ('reply.observation.created_by_script.reference', 'script'),
    ('reply.is_canonical_reply', 'is_canonical_reply'),
    ('reply.started', 'started'),
    ('reply.last_submit', 'finished'),
    ('reply.originally_collected_on', 'collected_on'),
    ('reply.id', 'reply.id'),

    ('reply.observation.dyad.user.username', 'username'),
    ('reply.observation.dyad.relates_to.user.username', 'relates_to_username'),

    ('reply.observation.dyad.id', 'membership'),
    ('reply.observation.dyad.condition.tag', 'condition'),
    ('reply.observation.dyad.study.slug', 'study'),

    ('reply.observation.dyad.date_randomised', 'randomised')
]


@contract
def _internal_fields(FIELD_MAP):
    """
    Function to split FIELD up because it can get updated by the form.
    :type FIELD_MAP: seq
    :rtype: seq
    """

    return zip(*FIELD_MAP)[0]


ANSWER_VALUES = ['question.q_type',
                 'question.variable_name',
                 'get_value_for_export',
                 ]


def fupdate(dic, new):
    """Updates a dict and returns dict + new vals; useful when building a new list of dicts."""
    dic.update(new)
    return dic


def renamekeys(dictionary, oldnew_namepairs):
    """Renames keys in a dictionary, deleting keys which don't have a new name.
    :type oldnew_namepairs: list(tuple)
    :rtype: dict
    """

    for old, new in oldnew_namepairs:
        if not new:
            del dictionary[old]

        if old != new:
            dictionary[new] = dictionary.get(old, None)
            del dictionary[old]

    return dictionary


def _format_dates_for_export_in_place(dictionary):
    for k, v in dictionary.items():
        if isinstance(v, datetime) or isinstance(v, date):
            dictionary[k] = v.isoformat()


def _encode_dict_to_unicode_in_place(d):
    """Take a dictionary where values are str or None and encode all strings as utf-8 in place."""
    for k, v in d.items():
        if isinstance(v, basestring):
            d[k] = v.encode('utf-8')


def write_dict_to_file(datadict, headings):
    """Export dict to a csv file"""

    thefile = tempfile.TemporaryFile()
    writer = csv.DictWriter(thefile, headings, extrasaction='ignore')
    writer.writerow(dict([(i, i) for i in headings]))

    _ = [_encode_dict_to_unicode_in_place(i) for i in datadict]
    _ = [_format_dates_for_export_in_place(i) for i in datadict]

    writer.writerows(datadict)
    thefile.seek(0)
    return thefile


def makefile_string():
    return "do syntax.do\n!/Applications/StatTransfer10/st data.dta data.sav\nexit"


def generate_syntax(template, questions, reference_study=None):
    """Return a string of stata syntax to format exported datafile for a given set of questions."""

    t = get_template(template)
    syntax = t.render(
        Context({'questions': questions, 'reference_study': reference_study})
    )
    return syntax


def _reshape_wide(answers, grouping, variable, value):
    """Take a list of dictionaries, group, and then reshape to wide."""

    group_key_fun = lambda a: a[grouping]
    grouped = [list(cs) for _, cs in itertools.groupby(answers, group_key_fun)]
    combined_rows = [fupdate(reply[0], dict([(i[variable], i[value])
                                             for i in reply])) for reply in grouped]
    # get rid of the original variable names because each dict is now keyed by these
    [i.pop(variable) for i in combined_rows]
    [i.pop(value) for i in combined_rows]
    return combined_rows


BOOLS_MAPPING = {False: 0, True: 1}

def _map_bools_to_ints(dic):
    """Make booleans 1/0 for export to SPSS/Stata"""
    for k, val in dic.iteritems():
        if type(val) == bool:
            dic[k] = BOOLS_MAPPING[val]

@contract
def values_with_callables(instance, keys):
    """Instead of calling values() on a queryset, call this instead to get access
    to callables on the model instances using dotted access syntax in keys

    :type instance: a
    :type keys: seq
    :rtype: dict
    """
    return {k: supergetattr(instance, k, None, call=True) for k in keys}


@contract
def build_csv_data_as_string(answers, reference_study):
    """
    :type answers: seq
    :type reference_study: b
    :rtype: string
    """

    # grab the answers with values processed for export
    answer_values = [values_with_callables(i, _internal_fields(FIELD_MAP))
        for i in answers]

    # map old names to new ones and reshape wide
    [renamekeys(i, FIELD_MAP) for i in answer_values]
    reply_dicts = _reshape_wide(answer_values, 'reply.id', 'variable_name', 'answer')
    [i.pop('qtype') for i in reply_dicts]

    # add some meta-data to each reply
    [reply.update({'is_reference_study': bool(reply['study'] == supergetattr(reference_study, "slug"))}) for reply in reply_dicts]
    [_map_bools_to_ints(d) for d in reply_dicts]

    headings = sorted(set(itertools.chain(*[i.keys() for i in reply_dicts])))
    data = write_dict_to_file(reply_dicts, headings).read()
    return data


def build_zipfile(answers, reference_study, zip_path):
    """Writes data and syntax to temporary directory and returns the path to that directory."""

    answers_with_files = answers.exclude(upload="")

    data = build_csv_data_as_string(tuple(answers), reference_study)

    syntax = generate_syntax('admin/ask/stata_syntax.html',
        Question.objects.filter(id__in=answers.values('question__id')),
        reference_study=reference_study)

    zipf = zipfile.ZipFile(zip_path, "w")

    syntax = smart_unicode(syntax).encode('utf-8')

    zipf.writestr('syntax.do', syntax)
    zipf.writestr('data.csv', data)
    zipf.writestr('make.do', makefile_string())
    [zipf.writestr("uploads" + i.upload.name, i.upload.read(
    ), ) for i in answers_with_files if i.upload.name]

    return zipf


@group_required(['Researchers', ])
def export_data(request):
    form = SelectExportDataForm(request.POST or None)
    if not form.is_valid():
        return render_to_response('manage/export_data.html', {'form': form},
                                  context_instance=RequestContext(request))

    return export_dataframe(form.cleaned_data['answers'], form.cleaned_data['reference_study'])


def export_dataframe(answers, reference_study):
    """Export the data one row per reply, along with stata syntax to import/label it, as a zip."""

    zip_path = "/tmp/export.zip"

    zipcontent = build_zipfile(
        answers,
        reference_study,
        zip_path
    )
    zipcontent.close()
    zipcontent = open(zip_path, 'r').read()
    os.remove(zip_path)

    # return httpresponse
    response = HttpResponse(
        zipcontent, content_type='application/x-zip-compressed')
    response['Content-disposition'] = "attachment; filename=exported_data.zip"
    return response


def _shifted(obj, datetimefield, delta):
    setattr(obj, datetimefield, getattr(obj, datetimefield) + delta)
    return obj


@group_required(['Researchers', ])
@conditional_decorator(revision.create_on_success, settings.USE_VERSIONING)
def dateshift_membership(request, pk=None):
    '''Allows Researchers to shift the time of all observations within a Membership.'''

    membership = get_object_or_404(Membership, id=pk)
    form = DateShiftForm(request.POST or None)
    if form.is_valid():

        # calclate difference from current randomisation date and shift randomisation date
        delta = form.delta(current=membership.date_randomised)
        membership.date_randomised = membership.date_randomised + delta
        membership.save()

        shiftable = [i for i in membership.observations(
        ) if i.timeshift_allowed()]
        shifted = [_shifted(i, 'due', delta) for i in shiftable]
        shifted = [_shifted(i, 'due_original', delta) for i in shiftable]

        _ = [i.add_data("timeshift", value=delta) for i in shifted]
        _ = [i.save() for i in shifted]

        if settings.USE_VERSIONING:
            revision.comment = "Timeshifted observations by %s" % (delta,)

        form = DateShiftForm(
        )  # wipe the form to make it harder to double-submit by accident
        messages.add_message(request, messages.WARNING,
            """%s observations shifted by %s (read this carefully and thoroughly, it can be
                confusing).""" % (len(shifted), delta))

    else:
        messages.add_message(request, messages.ERROR,
                             """Be careful with this form!!! Changes are saved as soon as you submit.""")

    return render_to_response('admin/signalbox/dateshift.html',
                              {'form': form, 'membership': membership}, context_instance=RequestContext(request))

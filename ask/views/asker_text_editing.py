"""
Module which handles importing questionnaires in special markdown dialect
using pandoc to process the input. Also see to_markdown functions on Question,
Asker etc for the reverse of this process.

This is somewhat a work in progress, and is for expert users only just now.

"""

from ask.models import Asker, ChoiceSet, Question, AskPage, Choice, ShowIf
from ask.models import question
from ask.views.pandochelpers import stringify, get_meta_tuple
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, HttpResponse
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext
from hashlib import sha1
from itertools import groupby, chain
from signalbox.models import Reply
from signalbox.decorators import group_required
from signalbox.utilities.djangobits import dict_map
import ast
import floppyforms as forms
import os
import re
from ask.yamlextras import yaml


def get_or_modify(klass, lookups, params):
    """:: Klass -> Dict -> Dict -> (KlassInstance, Bool)
    Takes
        - Class of the object
        - (id_field_name, id_value)
        - other params in a dictionary

    Returns
        - a new or modified object matching class + id, and with params set.
        - boolean indicating whether object was modified
          (modified objects are automatically saved)
    """
    ob, created = klass.objects.get_or_create(**lookups)
    mods = []
    klassfields = map(lambda x: getattr(x, "name"), klass.__dict__['_meta'].fields)
    for k, v in params.iteritems():
        if k in klassfields:  # ignore extra fields by default
            mods.append(not getattr(ob, k) == v)
            setattr(ob, k, v)
    modified = bool(sum(mods))
    ob.save()

    return ob, modified





def fix_showif_values(s):
    """Mainly used to identify lists in showif conditions"""
    try:
        return ast.literal_eval(s.strip())
    except:
        return s.strip()

# process textual showif conditions: use regex to split and
# make a dict containing variable_name, operator and value(s)
# note parens around operator split means the delimiter is retained
# in the result list
showif_to_dict = lambda s: dict_map(
    fix_showif_values,
    dict(zip("variable_name operator value".split(), re.split('(<|>|=|\sin\s)', s)))
)


def process_question_showif(question):
    if not question.get('showif', None):
        # do this to make sure the existing showif will get delinked
        question.update({'showif': None})
    else:
        showifdict = showif_to_dict(question['showif'])
        _fix_value = lambda v: isinstance(v, list) and ",".join(map(str, v)) or v and int(v) or None
        field_mapping = {
            '<': {'less_than': _fix_value(showifdict.get('value', None))},
            '>': {'more_than': _fix_value(showifdict.get('value', None))},
            '=': {'values': _fix_value(showifdict.get('value', None))},
            'in': {'values': _fix_value(showifdict.get('value', None))},
        }
        showifvalues = field_mapping[showifdict['operator']]
        showifvalues.update({'previous_question':
                get_or_modify(Question, {'variable_name': showifdict['variable_name']}, {})[0]})
        question.update({'showif': get_or_modify(ShowIf, showifvalues, {})[0]})



class YamlEditForm(forms.Form):

    yaml = forms.CharField(required=True,
        widget=forms.widgets.Textarea(attrs={'rows': 15}))

    def __init__(self, *args, **kwargs):
        self.asker = kwargs.pop('asker')
        initial = kwargs.get('initial', {})
        if not initial.get('yaml'):
            initial['yaml'] = self.asker.as_yaml()
        kwargs['initial'] = initial
        super(YamlEditForm, self).__init__(*args, **kwargs)

    def clean(self):
        documents = filter(bool, list(yaml.load_all(self.cleaned_data.get("yaml"))))
        try:
            askerdict = documents[0]['asker']
        except KeyError:
            raise forms.ValidationError("No information provided about the Asker")

        asker, _ = get_or_modify(Asker, {"id":self.asker.id}, askerdict)

        try:
            choicesetsdictlist = documents[-1]['choicesets']
            documents = documents[:-1]
        except KeyError:
            choicesetsdictlist = []

        # what is left are pages of questions
        pages = documents[1:]

        self.cleaned_data.update({
                "asker": asker,
                "choicesets": choicesetsdictlist,
                "pages": pages
            })

        return self.cleaned_data


    def save(self):

        asker = self.cleaned_data['asker']

        # delete preview replies to allow editing if we've been testing
        # otherwise db integrity errors
        Reply.objects.filter(asker=asker, entry_method="preview").delete()

        choicesets = self.cleaned_data['choicesets']
        pages = self.cleaned_data['pages']

        asker.askpage_set.all().delete()
        asker.save()

        askpages = [AskPage(asker=asker, order=i, step_name=p.keys()[0]) for i, p in enumerate(pages)]
        [i.save() for i in askpages]

        def _addchoices(choiceset, listofdicts):
            choices = []
            for i, kv in enumerate(listofdicts):
                k, v = kv.items()[0]
                cs = Choice(choiceset=choiceset, order=i, score=k, label=v)
                choices.append(cs)

            return choiceset


        choicesetsanditems = [(ChoiceSet.objects.get_or_create(name=k)[0], v)
            for k, v in choicesets.items()]

        x = [cs.add_choices_from_list_of_dicts(listofchoices)
            for cs, listofchoices in choicesetsanditems]


        for page, qs in zip(askpages, pages):
            pageqs = qs[page.step_name] # get the questions from the dict which is keyed by the step name

            def _liftqdict(qdict):
                variable_name, d = qdict.items()[0]
                d.update({'variable_name': variable_name})
                return d
            pageqs = map(_liftqdict, pageqs)
            [d.update({'order': i}) for i, d in enumerate(pageqs)]
            [d.update({'page': page}) for d in pageqs]

            def _lift_choiceset(d):
                if d.get('choiceset'):
                    cs, _ = ChoiceSet.objects.get_or_create(name=d.get('choiceset'))
                else:
                    cs = None
                d.update({'choiceset': cs})
                return d

            pageqs = [_lift_choiceset(d) for d in pageqs]

            ques_objs, _ = zip(*[get_or_modify(Question,
                {'variable_name': d['variable_name']}, d)
                for d in pageqs])

            [i.save() for i in ques_objs]

        return asker



@group_required(['Researchers', 'Research Assistants'])
def edit_yaml_asker(request, asker_id):
    asker = Asker.objects.get(id=asker_id)
    form = YamlEditForm(request.POST or None, asker=asker)

    if form.is_valid():
        form.save()
        return HttpResponseRedirect(reverse('edit_yaml_asker', args=(asker.id,)))

    return render_to_response(
        'admin/ask/text_asker_edit.html', {'form': form, 'asker': asker},
        context_instance=RequestContext(request))

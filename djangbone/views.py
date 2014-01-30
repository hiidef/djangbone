import datetime
import decimal
import json

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse, Http404
from django.views.generic import View


class DjangboneJSONEncoder(json.JSONEncoder):
    """
    JSON encoder that converts additional Python types to JSON.
    """
    def default(self, obj):
        """
        Converts datetime objects to ISO-compatible strings during json serialization.
        Converts Decimal objects to floats during json serialization.
        """
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        elif isinstance(obj, decimal.Decimal):
            return float(obj)
        else:
            return None


class BackboneAPIView(View):
    """
    Abstract class view, which makes it easy for subclasses to talk to backbone.js.

    Supported operations (copied from backbone.js docs):
        create -> POST   /collection
        read ->   GET    /collection[/id]
        update -> PUT    /collection/id
        delete -> DELETE /collection/id
    """
    base_queryset = None        # Queryset to use for all data accesses, eg. User.objects.all()
    serialize_fields = tuple()  # Tuple of field names that should appear in json output
    custom_values = {}          # A dict of functions to run for populating response fields, eg. {'rel_name': lambda x: x.related.name}
    values_override = None      # A single function to run to populate the whole response, eg. lambda x: x.backboneJSON

    # Field name to use as the primary key
    pk = 'id'

    # Optional pagination settings:
    page_size = None            # Set to an integer to enable GET pagination (at the specified page size)
    page_param_name = 'p'       # HTTP GET parameter to use for accessing pages (eg. /widgets?p=2)

    # Override these attributes with ModelForm instances to support PUT and POST requests:
    add_form_class = None       # Form class to be used for POST requests
    edit_form_class = None      # Form class to be used for PUT requests

    # Override these if you have custom JSON encoding/decoding needs:
    json_encoder = DjangboneJSONEncoder()
    json_decoder = json.JSONDecoder()

    def get(self, request, *args, **kwargs):
        """
        Handle GET requests, either for a single resource or a collection.
        """
        if kwargs.get(self.pk):
            return self.get_single_item(request, *args, **kwargs)
        else:
            return self.get_collection(request, *args, **kwargs)

    def get_single_item(self, request, *args, **kwargs):
        """
        Handle a GET request for a single model instance.
        """
        try:
            qs = self.base_queryset.filter(pk=kwargs[self.pk])
            assert len(qs) == 1
        except AssertionError:
            raise Http404
        output = self.serialize_qs(qs)
        return self.success_response(output)

    def get_collection(self, request, *args, **kwargs):
        """
        Handle a GET request for a full collection (when no id was provided).
        """
        qs = self.base_queryset
        output = self.serialize_qs(qs)
        return self.success_response(output)

    def post(self, request, *args, **kwargs):
        """
        Handle a POST request by adding a new model instance.

        This view will only do something if BackboneAPIView.add_form_class is specified
        by the subclass. This should be a ModelForm corresponding to the model used by
        base_queryset.

        Backbone.js will send the new object's attributes as json in the request body,
        so use our json decoder on it, rather than looking at request.POST.
        """
        if self.add_form_class == None:
            return HttpResponse('POST not supported', status=405)
        try:
            request_dict = self.json_decoder.decode(request.body)
        except ValueError:
            return HttpResponse('Invalid POST JSON', status=400)
        form = self.add_form_class(request_dict)
        if hasattr(form, 'set_request'):
            form.set_request(request)
        if form.is_valid():
            obj = form.save()
            return self.success_response(self.serialize_qs([obj], single_object=True))
        else:
            return self.validation_error_response(form.errors)

    def put(self, request, *args, **kwargs):
        """
        Handle a PUT request by editing an existing model.

        This view will only do something if BackboneAPIView.edit_form_class is specified
        by the subclass. This should be a ModelForm corresponding to the model used by
        base_queryset.
        """
        if self.edit_form_class == None or self.pk not in kwargs:
            return HttpResponse('PUT not supported', status=405)
        try:
            # Just like with POST requests, Backbone will send the object's data as json:
            request_dict = self.json_decoder.decode(request.body)
            instance = self.base_queryset.get(pk=kwargs[self.pk])
        except ValueError:
            return HttpResponse('Invalid PUT JSON', status=400)
        except ObjectDoesNotExist:
            raise Http404
        form = self.edit_form_class(request_dict, instance=instance)
        if hasattr(form, 'set_request'):
            form.set_request(request)
        if form.is_valid():
            obj = form.save()
            return self.success_response(self.serialize_qs([obj], single_object=True))
        else:
            return self.validation_error_response(form.errors)

    def delete(self, request, *args, **kwargs):
        """
        Respond to DELETE requests by deleting the model and returning its JSON representation.
        """
        if self.pk not in kwargs:
            return HttpResponse('DELETE is not supported for collections', status=405)
        qs = self.base_queryset.filter(pk=kwargs[self.pk])
        if qs:
            output = self.serialize_qs(qs)
            qs.delete()
            return self.success_response(output)
        else:
            raise Http404

    def serialize_qs(self, queryset, single_object=False):
        """
        Serialize a queryset into a JSON object that can be consumed by backbone.js.

        If the single_object argument is True, or the url specified an id, return a
        single JSON object, otherwise return a JSON array of objects.
        """
        if single_object or self.kwargs.get(self.pk):
            # For single-item requests, convert ValuesQueryset to a dict simply
            # by slicing the first item:
            data = self.get_values(queryset[:1])[0]
            json_output = self.json_encoder.encode(data)
        else:
            # Process pagination options if they are enabled:
            if isinstance(self.page_size, int):
                try:
                    page_number = int(self.request.GET.get(self.page_param_name, 1))
                    offset = (page_number - 1) * self.page_size
                except ValueError:
                    offset = 0
                queryset = queryset[offset:offset + self.page_size]
            json_output = self.json_encoder.encode(self.get_values(queryset))
        return json_output

    def get_values(self, qs):
        if self.values_override:
            values = [self.values_override(obj) for obj in qs]
        elif self.custom_values or not callable(getattr(qs, 'values', None)):
            values = []
            for obj in qs:
                vals = dict([(name, getattr(obj, name)) for name in self.serialize_fields])
                for name, method in self.custom_values.items():
                    vals[name] = method(obj)
                values.append(vals)
        else:
            values = list(qs.values(*self.serialize_fields))

        return values

    def success_response(self, output):
        """
        Convert json output to an HttpResponse object, with the correct mimetype.
        """
        return HttpResponse(output, mimetype='application/json')

    def validation_error_response(self, form_errors):
        """
        Return an HttpResponse indicating that input validation failed.

        The form_errors argument contains the contents of form.errors, and you
        can override this method is you want to use a specific error response format.
        By default, the output is a simple text response.
        """
        return HttpResponse('<p>ERROR: validation failed</p>' + str(form_errors), status=400)
